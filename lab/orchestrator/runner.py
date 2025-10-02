from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional
import logging
import time
import sys

from app.core.logger_setup import setup_logger
from app.core.ssh_manager import SSHManager
from lab.agents.attack import AttackExecutor
from lab.agents.sensor import SensorAgent

from lab.orchestrator.yaml_loader import ExperimentSpec, resolve_profile_command, _flatten, _safe_format

logger = setup_logger(Path('.logs'), name="[YamlLoader]")

@dataclass
class ExperimentRunner:
    def __init__(self, ssh_manager: SSHManager, lab_dir: Path):
        self.ssh = ssh_manager
        self.lab_dir = Path(lab_dir)

    # -----------------------
    # Utilidades internas
    # -----------------------
    def _guest_ip(self, name: str) -> str:
        try:
            script = r"""
                set -e
                ips=$(ip -o -4 addr show scope global | awk '{print $4}' | cut -d/ -f1)
                echo "$ips" | awk '/^192\.168\.56\./{print; found=1} END{ if(!found && NR>0) print $1 }' | head -n1
            """
            out = (self.ssh.run_command(name, script, timeout=25) or "").strip()
            return out
        except Exception as e:
            logger.warning(f"[Runner] guest_ip({name}) falhou: {e}")
            return ""

    def _resolve_ips(self, spec: ExperimentSpec) -> Dict[str, str]:
        ips: Dict[str, str] = {}
        for role in ("attacker", "victim", "sensor"):
            ip = self._guest_ip(role)
            logger.info(f"[Runner] {role} ip={ip}")
            ips[role] = ip
        return ips

    def _ensure_network_mode(self, spec: ExperimentSpec):
        mode = str((spec.network or {}).get("mode") or "").strip()
        if mode != "host_only":
            raise RuntimeError("network.mode != host_only — abortando por segurança.")
        logger.info("[Runner] network.mode OK (host_only).")

    # -----------------------
    # ETL acoplado ao Runner
    # -----------------------
    def _run_etl(self, exp_dir: Path, etl_out_root: Path) -> Optional[Path]:
        """
        Executa pré-ETL e ETL final.
        - pré-ETL gera features_conn_window.csv dentro de exp_dir
        - ETL final escreve datasets prontos em etl_out_root/<exp_id>/
        """
        try:
            logger.info(f"[ETL] Iniciando pipeline ETL (exp_dir={exp_dir})")

            # Import dinâmico e robusto (evita falhas se módulos estiverem em outro path)
            try:
                from lab.datasets.pre_etl import generate_conn_features
            except Exception as e:
                logger.warning(f"[ETL] import pre_etl falhou: {e} — tentando ajustar sys.path")
                sys.path.append(str(self.lab_dir))
                from lab.datasets.pre_etl import generate_conn_features

            try:
                from lab.datasets.etl_netsec import run_etl
            except Exception as e:
                logger.warning(f"[ETL] import etl_netsec falhou: {e} — tentando ajustar sys.path")
                sys.path.append(str(self.lab_dir))
                from lab.datasets.etl_netsec import run_etl

            # 1) Pré-ETL (janelas agregadas por conn.log, com timeline/heurística)
            try:
                out_csv = generate_conn_features(exp_dir, window_s=60)
                logger.info(f"[ETL] Pré-ETL ok: {out_csv}")
            except Exception as e:
                logger.warning(f"[ETL] Pré-ETL falhou (seguindo para ETL direto do Zeek): {e}")

            # 2) ETL final (datasets prontos para ML)
            exp_id = exp_dir.name
            etl_out_dir = Path(etl_out_root) / exp_id
            etl_out_dir.mkdir(parents=True, exist_ok=True)

            path_done = run_etl(exp_dir, etl_out_dir)
            logger.info(f"[ETL] Finalizado em: {path_done}")
            return Path(path_done)

        except Exception as e:
            logger.error(f"[ETL] Falha geral no ETL: {e}")
            return None

    # -----------------------
    # Execução do experimento
    # -----------------------
    def run(self, spec: ExperimentSpec, out_dir: Path, run_pre_etl: bool = True, cancel_event=None) -> str:
        t0 = time.time()
        exp_id = str((spec.experiment or {}).get("id") or "EXP")
        out_base = Path(out_dir) / exp_id
        out_base.mkdir(parents=True, exist_ok=True)

        sensor = SensorAgent(self.ssh, "sensor")
        attacker = AttackExecutor(self.ssh)
        ips: Dict[str, str] = {}

        logger.info(f"[Runner] início exp_id={exp_id} out={out_base} pre_etl={run_pre_etl}")

        def _cancelled() -> bool:
            try:
                return bool(cancel_event and cancel_event.is_set())
            except Exception:
                return False

        marker = out_base / "_runner_done.txt"
        err: Exception | None = None
        etl_executado = False

        try:
            for step in (spec.workflow or []):
                if _cancelled():
                    logger.warning("[Runner] cancelado — encerrando cedo.")
                    break

                logger.info(f"[Runner] step: {step.name}")
                for action in (step.actions or []):
                    if _cancelled():
                        logger.warning("[Runner] cancelado — interrompendo ações.")
                        break

                    # --- safety ---
                    if "ensure_network_mode" in action:
                        self._ensure_network_mode(spec)
                        continue

                    if "resolve_ips" in action:
                        ips = self._resolve_ips(spec)
                        continue

                    # --- sensor ---
                    if "start_sensor" in action:
                        logger.info("[Runner] iniciando sensor (tcpdump+zeek)…")
                        sensor.sanitize_and_start(
                            victim_ip=ips.get("victim", ""),
                            attacker_ip=ips.get("attacker", "")
                        )
                        continue

                    if "stop_sensor" in action:
                        sensor.stop()
                        continue

                    # --- ataque ---
                    if "run_profile" in action:
                        profile_id = str(action["run_profile"].get("profile_id"))
                        host, cmd, ssh_timeout = resolve_profile_command(spec, profile_id, ips)
                        logger.info(f"[Runner] run_profile={profile_id} on={host}\n---PROFILE CMD---\n{cmd}\n---END PROFILE CMD---")
                        attacker.run_cmd(host, cmd, timeout=ssh_timeout)

                        # verificação leve de saída Hydra (se nosso template padrão foi usado)
                        try:
                            local_lists = str((spec.gvars or {}).get("local_lists") or "$HOME/tcc/lists")
                            vic_ip = ips.get("victim", "")
                            verify = (
                                f'test -s "{local_lists}/hydra_{vic_ip}.out" '
                                f'&& echo "[verify] hydra output ok: {local_lists}/hydra_{vic_ip}.out" '
                                f'|| echo "[verify] hydra output MISSING"; '
                                f'tail -n 20 "{local_lists}/hydra_{vic_ip}.out" 2>/dev/null || true'
                            )
                            attacker.run_cmd(host, verify, timeout=20)
                        except Exception as e:
                            logger.warning(f"[Runner] verificação do hydra out falhou: {e}")
                        continue

                    # --- coleta ---
                    if "collect_artifacts" in action:
                        try:
                            sensor.collect_snapshot()
                        except Exception as e:
                            logger.warning(f"[Runner] snapshot: {e}")

                        # Manifesto do experimento
                        manifest = out_base / "manifest.txt"
                        manifest.write_text(
                            f"exp_id={exp_id}\nips={ips}\nstarted={t0}\nended={time.time()}\n",
                            encoding="utf-8"
                        )
                        logger.info(f"[Runner] manifest: {manifest}")

                        # ETL acoplado (gera datasets prontos em data/etl/<exp_id>/)
                        if run_pre_etl:
                            etl_root = Path(out_dir).parent / "lab" / "etl"
                            try:
                                etl_path = self._run_etl(out_base, etl_root)
                                etl_executado = etl_path is not None
                                if etl_executado:
                                    logger.info(f"[Runner] ETL pronto em {etl_path}")
                                else:
                                    logger.warn("[Runner] ETL não produziu saída (veja logs acima).")
                            except Exception as e:
                                logger.error(f"[Runner] ETL falhou: {e}")
                                etl_executado = False
                        continue

                    # --- cmd arbitrário pelo YAML (debug/etc) ---
                    if "run_cmd" in action:
                        host = str(action["run_cmd"].get("host") or "attacker")
                        raw = str(action["run_cmd"].get("cmd") or "")
                        ctx = {}
                        ctx.update(spec.gvars or {})
                        ctx.update(_flatten("experiment", spec.experiment or {}))
                        ctx.update({
                            "victim":   ips.get("victim", ""),
                            "attacker": ips.get("attacker", ""),
                            "sensor":   ips.get("sensor", "")
                        })
                        cmd = _safe_format(raw, ctx).replace("\r\n", "\n").replace("\r", "\n")
                        logger.info(f"[Runner] run_cmd on={host}\n---BEGIN CMD---\n{cmd}\n---END CMD---")
                        attacker.run_cmd(host, cmd, timeout=int((spec.gvars or {}).get("max_duration_s") or 900))
                        continue

                    logger.warning(f"[Runner] ação não reconhecida: {action}")

        except Exception as e:
            err = e
            logger.exception("[Runner] erro durante execução")
        finally:
            try:
                sensor.stop()
            except Exception:
                pass

            # Marker final
            try:
                status = "ok" if err is None else f"error:{type(err).__name__}"
                (out_base / "_runner_done.txt").write_text(f"{status} {time.time()}", encoding="utf-8")
            except Exception:
                logger.warning("[Runner] não foi possível criar marker final.")

            # Se o usuário desabilitar o run_pre_etl no futuro, ainda oferecemos um ETL ao final
            if (err is None) and (not etl_executado) and run_pre_etl:
                try:
                    etl_root = Path(out_dir).parent / "etl"
                    etl_path = self._run_etl(out_base, etl_root)
                    if etl_path:
                        logger.info(f"[Runner] (pós) ETL pronto em {etl_path}")
                except Exception as e:
                    logger.warning(f"[Runner] (pós) ETL falhou: {e}")

            logger.info(f"[Runner] fim em {time.time() - t0:.1f}s — artefato: {(out_base / '_runner_done.txt')}")

        if err is not None:
            raise err

        return str(out_base / "_runner_done.txt")
