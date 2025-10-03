from __future__ import annotations

import base64
import io
import json
import shlex
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
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
        self.timeline: list[dict] = []

    # -----------------------
    # Utilidades internas
    # -----------------------
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

    def _guest_ip(self, name: str) -> str:
        try:
            script = r"""
                set -e
                ips=$(ip -o -4 addr show scope global | awk '{print $4}' | cut -d/ -f1)
                echo "$ips" | awk '/^192\.168\.56\./{print; found=1} END{ if(!found && NR>0) print $1 }' | head -n1
            """.strip().replace("\n", "; ")
            wrapped = f"bash -lc {shlex.quote(script)}"
            out = (self.ssh.run_command(name, wrapped, timeout=25) or "").strip()
            return out
        except Exception as e:
            logger.warning(f"[Runner] guest_ip({name}) falhou: {e}")
            return ""

    def _pull_tree_b64(self, host: str, remote_dir: str, includes: list[str], local_dst: Path, timeout: int = 180):
        try:
            local_dst.mkdir(parents=True, exist_ok=True)
            inc = " ".join([f"\"{x}\"" for x in (includes or [])]) if includes else "."

            # Monta o script SEM adicionar ';' em duplicidade.
            script_lines = [
                "set -e",
                "set -o pipefail",
                f'REMOTEDIR="{remote_dir}"',
                'if [ -d "$REMOTEDIR" ]; then cd "$REMOTEDIR";',
                'elif [ -d "$HOME/tcc" ]; then cd "$HOME/tcc";',
                'elif [ -d "/home/vagrant/tcc" ]; then cd "/home/vagrant/tcc";',
                'elif [ -d "/tmp/tcc" ]; then cd "/tmp/tcc";',
                'else echo "::EMPTY::"; exit 0; fi',
                f'tar -czf - {inc} 2>/dev/null | (base64 -w0 2>/dev/null || base64)'
            ]

            # Junta com '; ' garantindo que não criamos ';;'
            script = "; ".join([ln.rstrip("; ") for ln in script_lines])

            wrapped = f"bash -lc {shlex.quote(script)}"
            logger.info(f"[Runner] pull {host}:{remote_dir} -> {local_dst}")

            b64 = self.ssh.run_command(host, wrapped, timeout=timeout) or ""
            if not b64.strip() or b64.strip() == "::EMPTY::":
                logger.warning(f"[Runner] nada para copiar de {host}:{remote_dir} (diretório remoto vazio/inexistente)")
                return

            data = base64.b64decode(b64.encode("utf-8"))
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
                tf.extractall(local_dst)

            logger.info(f"[Runner] ok: {local_dst}")
        except Exception as e:
            logger.error(f"[Runner] falha no pull {host}:{remote_dir} -> {local_dst}: {e}")

    def _write_metadata_and_timeline(self, out_base: Path, ips: dict, stages: list[dict]):
        try:
            meta = {
                "targets": {
                    "attacker_ip": ips.get("attacker", ""),
                    "victim_ip": ips.get("victim", ""),
                    "scan_ports": [22, 80, 8081]
                },
                "timeline": {"stages": stages},
                "generated_at": datetime.now(timezone.utc).isoformat()
            }
            (out_base / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            # timeline.json é útil para ETL relabel
            (out_base / "timeline.json").write_text(json.dumps({"stages": stages}, indent=2), encoding="utf-8")
            logger.info(f"[Runner] metadata/timeline escritos em {out_base}")
        except Exception as e:
            logger.warning(f"[Runner] metadata/timeline: {e}")

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
                out_csv = generate_conn_features(exp_dir, window_s=int(getattr(self, "pre_etl_window_s", 60)))
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

        try:
            self.pre_etl_window_s = int((spec.gvars or {}).get("pre_etl_window_s", 60))
            logger.info(f"[Runner] pre_etl_window_s={self.pre_etl_window_s}")
        except Exception:
            self.pre_etl_window_s = 60

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
                        tmpl_id = spec.profiles[profile_id].template
                        tmpl = spec.templates.get(tmpl_id)
                        label_base = (tmpl.label if tmpl and getattr(tmpl, "label", None) else f"profile_{profile_id}")

                        logger.info(
                            f"[Runner] run_profile={profile_id} on={host}\n---PROFILE CMD---\n{cmd}\n---END PROFILE CMD---")

                        t_start = datetime.now(timezone.utc).isoformat()
                        attacker.run_cmd(host, cmd, timeout=ssh_timeout)
                        t_end = datetime.now(timezone.utc).isoformat()

                        # timeline com base no label do template (sem forçar "Hydra" para tudo)
                        # Adiciona estágios no timeline com rótulo do template e, quando aplicável, um token padronizado
                        token = None
                        try:
                            if ("Hydra" in label_base) or profile_id.lower().startswith("hydra"):
                                token = "HydraBruteAction"
                            elif ("Nmap" in label_base) or profile_id.lower().startswith("nmap"):
                                token = "NmapScanAction"
                        except Exception:
                            token = None

                        base_stage = label_base if label_base else f"profile_{profile_id}"
                        self.timeline.append({"stage": f"{base_stage}_start", "ts": t_start})
                        self.timeline.append({"stage": f"{base_stage}_end", "ts": t_end})
                        if token:
                            self.timeline.append({"stage": f"{token}_start", "ts": t_start})
                            self.timeline.append({"stage": f"{token}_end", "ts": t_end})
# verificação do Hydra apenas quando for Hydra
                        try:
                            is_hydra = ("Hydra" in label_base) or profile_id.lower().startswith("hydra")
                            if is_hydra:
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
                            logger.warning(f"[Runner] verificação do hydra out falhou/ignorada: {e}")
                        continue

                    # --- coleta ---
                    if "collect_artifacts" in action:
                        try:
                            sensor.collect_snapshot()
                        except Exception as e:
                            logger.warning(f"[Runner] snapshot: {e}")

                        # Manifesto
                        manifest = out_base / "manifest.txt"
                        manifest.write_text(
                            f"exp_id={exp_id}\nips={ips}\nstarted={t0}\nended={time.time()}\n",
                            encoding="utf-8"
                        )
                        logger.info(f"[Runner] manifest: {manifest}")

                        # === NOVO: copiar artefatos das VMs ===
                        # Sensor: Zeek e PCAP (onde o sensor grava por padrão)
                        self._pull_tree_b64("sensor", "$HOME/tcc", ["zeek", "pcap", "run"], out_base / "sensor")
                        # Attacker: saída do Hydra (se existir)
                        try:
                            vic_ip = ips.get("victim", "")
                            self._pull_tree_b64("attacker", "$HOME/tcc",
                                                [f"lists/hydra_{vic_ip}.out", "lists/users.txt",
                                                 "lists/small_wordlist.txt"], out_base / "attacker")
                        except Exception as e:
                            logger.warning(f"[Runner] hydra logs: {e}")
                        # Victim: auth.log para reforçar brute/ssh
                        try:
                            self._pull_tree_b64("victim", "/var/log", ["auth.log", "auth.log.1"], out_base / "victim")
                        except Exception as e:
                            logger.warning(f"[Runner] auth.log: {e}")

                        # === NOVO: metadata/timeline para pré-ETL/ETL ===
                        self._write_metadata_and_timeline(out_base, ips, self.timeline)

                        # ETL acoplado (gera datasets prontos em data/etl/<exp_id>/)
                        if run_pre_etl:
                            # padronize a raiz de saída do ETL (use 'data/etl', não 'data/lab/etl')
                            etl_root = Path(out_dir).parent / "etl"
                            try:
                                etl_path = self._run_etl(out_base, etl_root)
                                if etl_path:
                                    logger.info(f"[Runner] ETL pronto em {etl_path}")
                                try:
                                    import json
                                    meta = Path(etl_path) / "meta" / "label_counts.json"
                                    if meta.exists():
                                        counts = json.loads(meta.read_text(encoding="utf-8"))
                                        logger.info(f"[Runner] Labels finais: {counts}")
                                except Exception as _e:
                                    logger.warning(f"[Runner] Falha lendo label_counts.json: {_e}")
                                else:
                                    logger.warning("[Runner] ETL não produziu saída (veja logs acima).")
                            except Exception as e:
                                logger.error(f"[Runner] ETL falhou: {e}")
                        continue

                    if "wait_seconds" in action:
                        try:
                            secs = int(action["wait_seconds"].get("seconds", 15))
                        except Exception:
                            secs = 15
                        logger.info(f"[Runner] aguardando {secs}s para consolidar logs…")
                        try:
                            time.sleep(secs)
                        except Exception as e:
                            logger.warning(f"[Runner] falha ao aguardar: {e}")
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
