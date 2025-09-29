import logging, time, json
import random
import subprocess
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed, CancelledError
from pathlib import Path

from lab.agents.attacker_agent import AttackerAgent
from lab.agents.sensor_agent import SensorAgent
from lab.agents.victim_agent import VictimAgent
from lab.capture.data_collector import DataCollector

logger = logging.getLogger("[Runner]")

# ---------------------------------------------
# SAFETY GATE (egress guard + NAT toggle)
# tenta importar de lab.security.safety e, em fallback, security.safety
try:
    from lab.security.safety import (
        apply_attacker_egress_guard,
        remove_attacker_egress_guard,
        toggle_attacker_nat,
    )
except Exception:
    try:
        from lab.security.safety import (
            apply_attacker_egress_guard,
            remove_attacker_egress_guard,
            toggle_attacker_nat,
        )
    except Exception as e:
        logger.warning(f"[Runner] safety.py não encontrado ({e}). Continuando SEM hardening de rede.")
        apply_attacker_egress_guard = None  # type: ignore
        remove_attacker_egress_guard = None  # type: ignore
        toggle_attacker_nat = None  # type: ignore
# ---------------------------------------------


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _resolve_workload(exp):
    """
    Lê exp.workload com defaults seguros.
    """
    wl = getattr(exp, "workload", {}) or {}
    return {
        "cycles": int(wl.get("cycles", 3)),
        "cool_down_min_s": int(wl.get("cool_down_min_s", 8)),
        "cool_down_max_s": int(wl.get("cool_down_max_s", 18)),
        "benign_burst_s": int(wl.get("benign_burst_s", 15)),
        "max_parallel_bruteforce": int(wl.get("max_parallel_bruteforce", 2)),
        "ssh_attempts": int(wl.get("ssh_attempts", 150)),
        "http_attempts": int(wl.get("http_attempts", 150)),
        "rate_limit_per_sec": int(wl.get("rate_limit_per_sec", 8)),
        "jitter_ms": wl.get("jitter_ms", [60, 180]),
    }

class ExperimentRunner:
    def __init__(self, ssh_manager, lab_dir: Path):
        self.ssh = ssh_manager
        self.lab_dir = Path(lab_dir)

    def _check_cancel(self, cancel_event: threading.Event | None):
        if cancel_event and cancel_event.is_set():
            raise CancelledError("cancel requested")

    def _with_retry(self, tries: int, delay_s: float, fn, *args, **kwargs):
        last = None
        for i in range(1, tries + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                last = e
                logger.error(f"[Runner] Retry {i}/{tries} após falha: {e}")
                time.sleep(delay_s * i)
        raise last if last else RuntimeError("Falha sem exceção?")

    def _stop_sensor_capture_best_effort(self):
        """
        Para tcpdump/zeek no sensor com segurança e sem derrubar o shell:
        - valida PIDs lidos de pidfiles (kill -0)
        - envia TERM e, se necessário, KILL
        - usa pkill -x (nome exato), não -f
        - limpa pidfiles (inclusive órfãos)
        """
        try:
            self.ssh.run_command(
                "sensor",
                "bash -lc '"
                "shopt -s nullglob; "
                # 1ª passada: TERM em PIDs válidos
                "for p in /var/run/tcc_*.pid; do "
                "  [ -s \"$p\" ] || continue; "
                "  pid=\"$(tr -cd 0-9 < \"$p\")\"; "
                "  if [ -n \"$pid\" ] && sudo -n kill -0 \"$pid\" 2>/dev/null; then "
                "    sudo -n kill \"$pid\" 2>/dev/null || true; "
                "  fi; "
                "done; "
                # Mata por nome EXATO (não derruba o bash executor)
                "sudo -n pkill -x tcpdump 2>/dev/null || true; "
                "sudo -n pkill -x zeek    2>/dev/null || true; "
                "sleep 0.3; "
                # 2ª passada: KILL se ainda vivo + limpa pidfiles
                "for p in /var/run/tcc_*.pid; do "
                "  [ -e \"$p\" ] || continue; "
                "  pid=\"$(tr -cd 0-9 < \"$p\")\"; "
                "  if [ -n \"$pid\" ] && sudo -n kill -0 \"$pid\" 2>/dev/null; then "
                "    sudo -n kill -9 \"$pid\" 2>/dev/null || true; "
                "  fi; "
                "  sudo -n rm -f \"$p\" 2>/dev/null || true; "
                "done'"
                ,
                timeout=40
            )
            logger.info("[Runner] Captura no sensor: parada com sucesso (TERM/KILL best-effort).")
        except Exception as e:
            logger.warning(f"[Runner] Falha ao parar captura (best-effort): {e}")

    def _write_status_marker(self, out_dir: Path, exp_id: str, timeline: dict, status: str):
        try:
            base_dir = Path(out_dir) / exp_id
            base_dir.mkdir(parents=True, exist_ok=True)
            (base_dir / "_run_status.json").write_text(
                json.dumps({"status": status}, indent=2), encoding="utf-8"
            )
            (base_dir / "timeline.json").write_text(
                json.dumps(timeline, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"[Runner] Falha ao salvar marcadores de execução: {e}")

    def run(self, exp, out_dir: Path, run_pre_etl: bool = True, cancel_event: threading.Event | None = None) -> Path:
        """
        Executa o experimento com suporte a cancelamento e rollback.
        - cancel_event: se setado externamente, aborta etapas com CancelledError.
        """
        timeline = {"exp_id": exp.exp_id, "stages": []}
        status = "error"   # assume erro até concluir OK
        aborted = False

        def mark(stage, extra=None):
            d = {"stage": stage, "ts": now_utc_iso()}
            if extra:
                d.update(extra)
            timeline["stages"].append(d)

        capture_on = False
        nat_off = False

        try:
            logger.info(f"[Runner] Iniciando experimento: {exp.exp_id}")
            mark("prepare_start")

            # Warm-up SSH das VMs
            mark("ssh_warm_start")
            self._check_cancel(cancel_event)
            self._warm_all()
            mark("ssh_warm_end")

            sensor = SensorAgent(self.ssh)
            victim = VictimAgent(self.ssh)
            attacker = AttackerAgent(self.ssh)

            # --- PREPARE (paralelo só para victim/attacker) ---
            self._check_cancel(cancel_event)
            with ThreadPoolExecutor(max_workers=2) as ex:
                futs = [
                    ex.submit(victim.prepare_services),
                    ex.submit(attacker.ensure_tools),
                ]
                for f in as_completed(futs):
                    self._check_cancel(cancel_event)
                    f.result()
            mark("prepare_end")

            # Em alguns casos o SSH do attacker reinicia após installs
            self._check_cancel(cancel_event)
            self._wait_vm_ssh("attacker", attempts=8, delay_s=5)

            # --- SENSOR: promíscuo + ferramentas em série ---
            self._check_cancel(cancel_event)
            logger.info("[Runner] Preparando sensor (promisc + ferramentas)...")
            mark("sensor_promisc_start")
            try:
                sensor.enable_promisc(iface_hint="eth1")
                mark("sensor_promisc_end", {"status": "ok"})
            except Exception as e:
                logger.warning(f"[Runner] enable_promisc falhou: {e}")
                mark("sensor_promisc_end", {"status": "error", "error": str(e)})

            mark("sensor_tools_start")
            try:
                sensor.ensure_tools()
                mark("sensor_tools_end", {"status": "ok"})
            except Exception as e:
                logger.error(f"[Runner] ensure_tools (sensor) falhou: {e}")
                mark("sensor_tools_end", {"status": "error", "error": str(e)})
                raise

            # --- Teste rápido de espelhamento (opcional) ---
            victim_ip = exp.targets.get("victim_ip") if isinstance(exp.targets, dict) else None
            try:
                if victim_ip:
                    ok_mirror = sensor.mirror_smoke_test(victim_ip=victim_ip, packets=6, iface_hint="eth1")
                    if not ok_mirror:
                        logger.warning("[Runner] mirror_smoke_test não capturou pacotes. Verifique nicpromisc no VirtualBox e gere tráfego.")
                else:
                    logger.warning("[Runner] victim_ip ausente no exp.targets; pulando mirror_smoke_test.")
            except Exception as e:
                logger.warning(f"[Runner] mirror_smoke_test falhou: {e}")

            # --- ARM capture ---
            self._check_cancel(cancel_event)
            mark("arm_start")
            sensor.arm_capture(
                rotate_sec=exp.capture_plan.rotate_seconds,
                rotate_mb=exp.capture_plan.rotate_size_mb,
                victim_ip=exp.targets.get("victim_ip"),
                attacker_ip=exp.targets.get("attacker_ip")
            )
            capture_on = True
            time.sleep(3)
            if not sensor.health():
                logger.warning("[Runner] Sensor sem logs recentes — prosseguindo para gerar eventos.")
            mark("arm_end")

            # SAFETY GATE: confina tráfego do atacante ao LAB
            self._check_cancel(cancel_event)
            sensor_ip = exp.targets.get("sensor_ip") if isinstance(exp.targets, dict) else None

            hardening_enabled = bool(toggle_attacker_nat)
            if hardening_enabled:
                mark("nat_isolate_start", {"victim_ip": victim_ip, "sensor_ip": sensor_ip})
                try:
                    self._with_retry(
                        tries=3,
                        delay_s=2,
                        fn=toggle_attacker_nat,
                        ssh=self.ssh,
                        enable=False,
                        victim_ip=victim_ip,
                        sensor_ip=sensor_ip
                    )
                    nat_off = True
                    mark("nat_isolate_end", {"status": "ok"})
                except Exception as e:
                    logger.warning(f"[Runner] Falha ao isolar NAT/egress: {e}")
                    mark("nat_isolate_end", {"status": "error", "error": str(e)})

            # ATTACK
            self._check_cancel(cancel_event)
            workload = _resolve_workload(exp)
            cycles = workload["cycles"]
            cool_min = workload["cool_down_min_s"]
            cool_max = workload["cool_down_max_s"]

            for c in range(1, cycles + 1):
                self._check_cancel(cancel_event)
                logger.info(f"[Runner] Iniciando ciclo {c}/{cycles}")

                try:
                    attacker.start_benign_burst(
                        benign_cfg=getattr(exp, "benign", None),
                        duration_s=workload["benign_burst_s"],
                        cancel_event=cancel_event
                    )
                except Exception as e:
                    logger.warning(f"[Runner] Benign burst falhou (ciclo {c}): {e}")

                for action in exp.actions:
                    self._check_cancel(cancel_event)
                    stage = f"attack_{action.__class__.__name__}"
                    mark(stage + "_start", {"cycle": c})
                    logger.info(f"[Runner] Ação: {action.__class__.__name__} (ciclo {c})")
                    action.run(
                        self.ssh,
                        victim_ip or exp.targets["victim_ip"],
                        workload=workload
                    )
                    mark(stage + "_end", {"cycle": c})

                    sleep_s = random.randint(cool_min, cool_max)
                    time.sleep(sleep_s)

                logger.info(f"[Runner] Ciclo {c} finalizado.")

            # VALIDATE
            self._check_cancel(cancel_event)
            mark("validate_start")
            auth_tail = victim.tail_auth(20)
            if auth_tail:
                logger.info(f"[Runner] auth.log (trecho):\n{auth_tail}")
            mark("validate_end")

            # --- PERSISTE TIMELINE ANTES DO ETL/harvest ---
            try:
                logger.info("[Runner] Persistindo timeline antes do ETL...")
                self._write_status_marker(out_dir, exp.exp_id, timeline, status="pre_etl")
                logger.info("[Runner] timeline.json gravado (pré-ETL).")
            except Exception as e:
                logger.error(f"[Runner] Falha ao persistir timeline antes do ETL: {e}")

            # HARVEST + PACKAGE
            self._check_cancel(cancel_event)
            mark("harvest_start")
            collector = DataCollector(self.ssh, self.lab_dir)
            zip_path = collector.harvest(exp.exp_id, out_dir, timeline=timeline, run_pre_etl=run_pre_etl)
            mark("harvest_end")

            logger.info(f"[Runner] Experimento finalizado: {zip_path}")
            status = "ok"
            return zip_path

        except CancelledError:
            aborted = True
            logger.error("[Runner] Execução cancelada pelo usuário.")
            raise
        except Exception as e:
            logger.error(f"[Runner] Falha geral do experimento: {e}")
            raise
        finally:
            try:
                if capture_on:
                    self._stop_sensor_capture_best_effort()
            except Exception:
                pass
            try:
                if nat_off and toggle_attacker_nat:
                    try:
                        toggle_attacker_nat(self.ssh, enable=True)
                        logger.info("[Runner] NAT/egress guard restaurado (removido).")
                    except Exception as e:
                        logger.warning(f"[Runner] Falha ao restaurar NAT/egress: {e}")
            except Exception:
                pass

            try:
                final_status = "aborted" if aborted else status
                self._write_status_marker(out_dir, exp.exp_id, timeline, final_status)
            except Exception:
                pass

    def _prepare_attacker_tools(self):
        try:
            logger.info("[Runner] Preparando ferramentas no atacante...")
            attacker = AttackerAgent(self.ssh)
            attacker.ensure_tools()
            logger.info("[Runner] Atacante pronto (ferramentas instaladas).")
        except Exception as e:
            logger.error(f"[Runner] Falha preparando atacante: {e}")
            raise

    def _wait_vm_ssh(self, name: str, attempts: int = 12, delay_s: int = 5):
        for i in range(1, attempts + 1):
            try:
                logger.info(f"[Runner] Aguardando SSH de {name} (tentativa {i}/{attempts})...")
                subprocess.check_call(
                    ["vagrant", "ssh", name, "-c", "true"],
                    cwd=self.lab_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                logger.info(f"[Runner] SSH pronto em {name}.")
                return
            except Exception as e:
                logger.warning(f"[Runner] SSH ainda não pronto em {name}: {e}")
                time.sleep(delay_s)
        raise RuntimeError(f"Timeout aguardando SSH de {name}.")

    def _warm_all(self):
        for n in ("attacker", "sensor", "victim"):
            self._wait_vm_ssh(n, attempts=12, delay_s=5)
