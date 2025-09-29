import logging, json, hashlib, shutil, os, subprocess
from pathlib import Path

from lab.datasets.etl_netsec import run_etl

logger = logging.getLogger("[DataCollector]")

class DataCollector:
    def __init__(self, ssh, lab_dir: Path):
        self.ssh = ssh
        self.lab_dir = Path(lab_dir)

    def _scp(self, host: str, remote: str, local: Path):
        """
        Cópia (um arquivo remoto -> destino local) em modo batch (sem prompts).
        Levanta exceção se falhar.
        """
        try:
            fields = self.ssh.get_ssh_fields(host)
            if local.exists() and local.is_dir():
                local.mkdir(parents=True, exist_ok=True)
                local_parent = local
            else:
                local_parent = local.parent
                local_parent.mkdir(parents=True, exist_ok=True)

            key   = fields["IdentityFile"]
            port  = fields["Port"]
            user  = fields["User"]
            hostn = fields["HostName"]

            cmd = [
                "scp",
                "-P", str(port),
                "-i", key,
                "-o", "BatchMode=yes",                        # não pedir senha
                "-o", "StrictHostKeyChecking=no",            # não perguntar yes/no
                "-o", "UserKnownHostsFile=/dev/null",        # não sujar known_hosts
                "-o", "PreferredAuthentications=publickey",  # não tentar password
                "-o", "ConnectTimeout=12",                   # não travar indefinidamente
                f"{user}@{hostn}:{remote}",
                str(local)
            ]
            logger.info("[SCP] %s", " ".join(cmd))
            res = subprocess.run(
                cmd, cwd=str(self.lab_dir),
                capture_output=True, text=True, timeout=60
            )
            if res.returncode != 0:
                logger.error("[SCP] Falhou (%s): %s", res.returncode, (res.stderr or "").strip())
                raise RuntimeError(f"SCP falhou ({res.returncode})")
            if res.stdout:
                logger.info("[SCP] %s", res.stdout.strip())
        except subprocess.TimeoutExpired:
            logger.error("[SCP] TIMEOUT")
            raise
        except Exception as e:
            logger.error(f"[SCP] Falhou: {e}")
            raise

    def _list_remote_glob(self, host: str, pattern: str):
        """
        Lista, no remoto, arquivos que casem com 'pattern' (nullglob ativado).
        """
        try:
            cmd = "bash -lc 'shopt -s nullglob dotglob; for f in " + pattern + "; do printf \"%s\\n\" \"$f\"; done'"
            out = self.ssh.run_command(host, cmd, timeout=10)
            matches = [l.strip() for l in out.splitlines() if l.strip()]
            return matches
        except Exception as e:
            logger.error(f"[SCP] Falha ao listar glob no remoto ({host}:{pattern}): {e}")
            return []

    def _scp_glob_optional(self, host: str, pattern: str, local_dir: Path) -> int:
        """
        Copia zero ou mais arquivos que casem com 'pattern' no remoto.
        Se nenhum arquivo existir, apenas loga e segue.
        Retorna a contagem de arquivos copiados.
        """
        try:
            local_dir.mkdir(parents=True, exist_ok=True)
            matches = self._list_remote_glob(host, pattern)
            if not matches:
                logger.warning(f"[SCP] Nenhum arquivo correspondente a {pattern} em {host}.")
                return 0
            count = 0
            for remote_path in matches:
                try:
                    # Destino é diretório local; scp tratará como diretório
                    self._scp(host, remote_path, local_dir)
                    count += 1
                except Exception as e:
                    logger.error(f"[SCP] Falha ao copiar {host}:{remote_path} -> {local_dir}: {e}")
            return count
        except Exception as e:
            logger.error(f"[SCP] Glob opcional falhou ({host}:{pattern}): {e}")
            return 0

    def harvest(self, exp_id: str, out_base: Path, timeline=None, run_pre_etl: bool = True) -> Path:
        """
        Coleta artefatos (Zeek/PCAP/auth/hydra/nmap), gera ETL e zipa o dataset.
        """
        base = Path(out_base) / exp_id
        sensor_pcap = base / "sensor" / "pcap"
        sensor_zeek = base / "sensor" / "zeek"
        victim_dir  = base / "victim"
        attacker_dir= base / "attacker"

        try:
            # SENSOR — Zeek (opcional) e PCAP (opcional)
            copied_zeek = self._scp_glob_optional("sensor", "/var/log/zeek/*.log", sensor_zeek)
            copied_pcap = self._scp_glob_optional("sensor", "/var/log/pcap/*.pcap", sensor_pcap)
            if copied_zeek == 0 and copied_pcap == 0:
                logger.error("[HARVEST] Sensor sem artefatos (Zeek/PCAP). Dataset seguirá mesmo assim.")

            # VICTIM — auth.log (opcional)
            try:
                self._scp("victim", "/var/log/auth.log", victim_dir / "auth.log")
            except Exception as e:
                logger.error(f"[HARVEST] auth.log ausente ou inacessível na vítima (seguindo): {e}")

            # ATTACKER — artefatos das ações
            # nmap (ex.: ~/exp_nmap/scan, etc)
            self._scp_glob_optional("attacker", "~/exp_nmap/*", attacker_dir)
            # hydra (ex.: ~/exp_brute/hydra_ssh.log)
            self._scp_glob_optional("attacker", "~/exp_brute/*", attacker_dir)
            # outros (DoS, etc., se existirem)
            self._scp_glob_optional("attacker", "~/exp_dos*", attacker_dir)

            # METADATA
            meta = base / "metadata.json"
            metadata = self._metadata(exp_id, timeline=timeline)
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

            # CHECKSUMS
            checksums = self._sha256_dir(base)
            (base / "checksums.sha256").write_text("\n".join(checksums), encoding="utf-8")

            # OPTIONAL: ETL
            if run_pre_etl:
                etl_ok = False
                try:
                    proc_dir = Path(out_base) / "processed" / exp_id
                    proc_dir.mkdir(parents=True, exist_ok=True)
                    out_proc = run_etl(base, proc_dir)
                    logger.info(f"[HARVEST] ETL completo gerado em: {out_proc}")
                    etl_ok = True
                except Exception as e:
                    logger.error(f"[HARVEST] ETL completo falhou, tentando pré-ETL antigo: {e}")

                if not etl_ok:
                    try:
                        from lab.datasets.pre_etl import generate_conn_features
                        csv_path = generate_conn_features(dataset_root=base)
                        logger.info(f"[HARVEST] Pré-ETL gerado: {csv_path}")
                    except Exception as e:
                        logger.error(f"[HARVEST] Pré-ETL falhou (seguindo sem bloquear): {e}")

                # Relatório leve do run
                try:
                    if etl_ok:
                        meta_dir = (Path(out_base) / "processed" / exp_id / "meta")
                        counts_json = meta_dir / "label_counts.json"
                        counts = {}
                        if counts_json.exists():
                            import json as _json
                            counts = _json.loads(counts_json.read_text(encoding="utf-8"))
                        report = {
                            "exp_id": exp_id,
                            "etl": "full" if etl_ok else "pre",
                            "labels": counts,
                            "artifacts": {
                                "processed": str(Path(out_base) / "processed" / exp_id),
                                "raw_base": str(base)
                            }
                        }
                        (base / "run_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
                except Exception as e:
                    logger.error(f"[HARVEST] Falha ao escrever run_report.json: {e}")

            # ZIP
            zip_path = base.with_suffix(".zip")
            if zip_path.exists():
                zip_path.unlink()
            shutil.make_archive(str(base), "zip", root_dir=base)
            logger.info(f"[HARVEST] Dataset empacotado: {zip_path}")
            return zip_path
        except Exception as e:
            logger.error(f"[HARVEST] Erro: {e}")
            raise

    def _sha256_dir(self, path: Path):
        out = []
        for root, _, files in os.walk(path):
            for f in files:
                fp = Path(root) / f
                h = hashlib.sha256()
                try:
                    with open(fp, "rb") as r:
                        for chunk in iter(lambda: r.read(1024*1024), b""):
                            h.update(chunk)
                    out.append(f"{h.hexdigest()}  {fp.relative_to(path)}")
                except Exception as e:
                    logger.error(f"[SHA256] Falha ao ler {fp}: {e}")
        return out

    def _metadata(self, exp_id: str, timeline=None) -> dict:
        try:
            kernel_victim   = self.ssh.run_command("victim",   "uname -a", timeout=5).strip()
            kernel_sensor   = self.ssh.run_command("sensor",   "uname -a", timeout=5).strip()
            kernel_attacker = self.ssh.run_command("attacker", "uname -a", timeout=5).strip()
        except Exception as e:
            logger.error(f"[META] Falha ao coletar uname: {e}")
            kernel_victim = kernel_sensor = kernel_attacker = "unknown"
        return {
            "exp_id": exp_id,
            "timeline": timeline or {},
            "guests": {
                "victim":   {"uname": kernel_victim},
                "sensor":   {"uname": kernel_sensor},
                "attacker": {"uname": kernel_attacker}
            },
            "tools": {
                "sensor": ["tcpdump","zeek"],
                "attacker": ["nmap","hydra","slowhttptest","hping3"]
            }
        }
