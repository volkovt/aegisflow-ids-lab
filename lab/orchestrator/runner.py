import logging, time, json
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
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


class ExperimentRunner:
    def __init__(self, ssh_manager, lab_dir: Path):
        self.ssh = ssh_manager
        self.lab_dir = Path(lab_dir)

    def run(self, exp, out_dir: Path, run_pre_etl: bool = True) -> Path:
        timeline = {"exp_id": exp.exp_id, "stages": []}

        def mark(stage, extra=None):
            d = {"stage": stage, "ts": now_utc_iso()}
            if extra:
                d.update(extra)
            timeline["stages"].append(d)

        try:
            logger.info(f"[Runner] Iniciando experimento: {exp.exp_id}")
            mark("prepare_start")

            sensor = SensorAgent(self.ssh)
            victim = VictimAgent(self.ssh)
            attacker = AttackerAgent(self.ssh)

            # PREPARE (instala ferramentas/serviços em paralelo)
            with ThreadPoolExecutor(max_workers=3) as ex:
                futs = [
                    ex.submit(sensor.ensure_tools),
                    ex.submit(victim.prepare_services),
                    ex.submit(attacker.ensure_tools),
                ]
                for f in as_completed(futs):
                    f.result()
            mark("prepare_end")

            # ARM capture
            mark("arm_start")
            sensor.arm_capture(
                rotate_sec=exp.capture_plan.rotate_seconds,
                rotate_mb=exp.capture_plan.rotate_size_mb,
                zeek_rotate_sec=exp.capture_plan.zeek_rotate_seconds
            )
            if not sensor.health():
                logger.warning("[Runner] Sensor sem logs recentes — prosseguindo para gerar eventos.")
            mark("arm_end")

            # -------------------------------------------------
            # SAFETY GATE: confina tráfego do atacante ao LAB
            victim_ip = exp.targets.get("victim_ip") if isinstance(exp.targets, dict) else None
            sensor_ip = exp.targets.get("sensor_ip") if isinstance(exp.targets, dict) else None

            hardening_enabled = bool(apply_attacker_egress_guard) or bool(toggle_attacker_nat)
            if hardening_enabled:
                # aplica egress guard
                if apply_attacker_egress_guard and victim_ip:
                    mark("safety_guard_apply_start", {"victim_ip": victim_ip, "sensor_ip": sensor_ip})
                    try:
                        apply_attacker_egress_guard(self.ssh, victim_ip=victim_ip, sensor_ip=sensor_ip)
                        mark("safety_guard_apply_end", {"status": "ok"})
                    except Exception as e:
                        logger.error(f"[Runner] Falha ao aplicar egress guard: {e}")
                        mark("safety_guard_apply_end", {"status": "error", "error": str(e)})

                # isola NAT do atacante durante os ataques (opcional)
                if toggle_attacker_nat:
                    mark("nat_isolate_start")
                    try:
                        toggle_attacker_nat(self.ssh, enable=False)
                        mark("nat_isolate_end", {"status": "ok"})
                    except Exception as e:
                        logger.warning(f"[Runner] Falha ao desativar NAT: {e}")
                        mark("nat_isolate_end", {"status": "error", "error": str(e)})
            # -------------------------------------------------

            try:
                # ATTACK
                for action in exp.actions:
                    stage = f"attack_{action.__class__.__name__}"
                    mark(stage + "_start")
                    logger.info(f"[Runner] Ação: {action.__class__.__name__}")
                    action.run(self.ssh, victim_ip or exp.targets["victim_ip"])
                    mark(stage + "_end")
            finally:
                # Sempre reverter hardening, mesmo em caso de erro
                if hardening_enabled:
                    if toggle_attacker_nat:
                        mark("nat_restore_start")
                        try:
                            toggle_attacker_nat(self.ssh, enable=True)
                            mark("nat_restore_end", {"status": "ok"})
                        except Exception as e:
                            logger.warning(f"[Runner] Falha ao reativar NAT: {e}")
                            mark("nat_restore_end", {"status": "error", "error": str(e)})

                    if remove_attacker_egress_guard:
                        mark("safety_guard_remove_start")
                        try:
                            remove_attacker_egress_guard(self.ssh)
                            mark("safety_guard_remove_end", {"status": "ok"})
                        except Exception as e:
                            logger.warning(f"[Runner] Falha ao remover egress guard: {e}")
                            mark("safety_guard_remove_end", {"status": "error", "error": str(e)})

            # VALIDATE simples
            mark("validate_start")
            auth_tail = victim.tail_auth(20)
            if auth_tail:
                logger.info(f"[Runner] auth.log (trecho):\n{auth_tail}")
            mark("validate_end")

            # HARVEST + PACKAGE
            mark("harvest_start")
            collector = DataCollector(self.ssh, self.lab_dir)
            zip_path = collector.harvest(exp.exp_id, out_dir, timeline=timeline, run_pre_etl=run_pre_etl)
            mark("harvest_end")

            # Guardar timeline standalone também
            tl_file = (Path(out_dir) / exp.exp_id / "timeline.json")
            try:
                tl_file.parent.mkdir(parents=True, exist_ok=True)
                tl_file.write_text(json.dumps(timeline, indent=2), encoding="utf-8")
            except Exception as e:
                logger.warning(f"[Runner] Falha ao salvar timeline.json: {e}")

            logger.info(f"[Runner] Experimento finalizado: {zip_path}")
            return zip_path
        except Exception as e:
            logger.error(f"[Runner] Falha geral do experimento: {e}")
            raise
