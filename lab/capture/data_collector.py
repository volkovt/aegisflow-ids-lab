import logging, json, hashlib, shutil, os, subprocess
from pathlib import Path

logger = logging.getLogger("[DataCollector]")

class DataCollector:
    def __init__(self, ssh, lab_dir: Path):
        self.ssh = ssh
        self.lab_dir = Path(lab_dir)

    def _scp(self, host: str, remote: str, local: Path):
        """
        Cópia simples (um arquivo remoto -> destino local).
        Mantém comportamento antigo e levanta exceção se falhar.
        """
        try:
            fields = self.ssh.get_ssh_fields(host)
            # Garante diretório-alvo (se 'local' for arquivo) ou o próprio diretório (se existir)
            if local.exists() and local.is_dir():
                local.mkdir(parents=True, exist_ok=True)
                local_parent = local
            else:
                local_parent = local.parent
                local_parent.mkdir(parents=True, exist_ok=True)

            key = fields["IdentityFile"]
            port = fields["Port"]
            user = fields["User"]
            hostn = fields["HostName"]
            cmd = ["scp", "-P", str(port), "-i", key, f"{user}@{hostn}:{remote}", str(local)]
            logger.info(f"[SCP] {' '.join(cmd)}")
            subprocess.check_call(cmd, cwd=self.lab_dir)
        except Exception as e:
            logger.error(f"[SCP] Falhou: {e}")
            raise

    def _list_remote_glob(self, host: str, pattern: str):
        """
        Lista, no remoto, os arquivos que correspondem ao 'pattern'.
        Usa nullglob para que 'nenhuma correspondência' não gere erro.
        """
        try:
            cmd = f"bash -lc 'shopt -s nullglob dotglob; for f in {pattern}; do printf \"%s\\n\" \"$f\"; done'"
            out = self.ssh.run_command(host, cmd, timeout=10)
            matches = [l.strip() for l in out.splitlines() if l.strip()]
            return matches
        except Exception as e:
            logger.error(f"[SCP] Falha ao listar glob no remoto ({host}:{pattern}): {e}")
            return []

    def _scp_glob_optional(self, host: str, pattern: str, local_dir: Path) -> int:
        """
        Copia zero ou mais arquivos que casem com 'pattern' no remoto.
        Se nenhum arquivo existir, apenas loga um aviso e segue em frente.
        Retorna a contagem de arquivos copiados.
        """
        try:
            local_dir.mkdir(parents=True, exist_ok=True)
            matches = self._list_remote_glob(host, pattern)
            if not matches:
                logger.error(f"[SCP] Nenhum arquivo correspondente a {pattern} em {host}.")
                return 0
            count = 0
            for remote_path in matches:
                try:
                    # Destino é o diretório local; como já existe, scp tratará como diretório
                    self._scp(host, remote_path, local_dir)
                    count += 1
                except Exception as e:
                    logger.error(f"[SCP] Falha ao copiar {host}:{remote_path} -> {local_dir}: {e}")
            return count
        except Exception as e:
            logger.error(f"[SCP] Glob opcional falhou ({host}:{pattern}): {e}")
            return 0

    def harvest(self, exp_id: str, out_base: Path, timeline=None, run_pre_etl: bool = True) -> Path:
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

            # ATTACKER — artefatos de ações (opcionais)
            self._scp_glob_optional("attacker", "~/exp_scan.nmap", attacker_dir)
            self._scp_glob_optional("attacker", "~/exp_brute.hydra", attacker_dir)
            self._scp_glob_optional("attacker", "~/exp_dos*", attacker_dir)

            # METADATA
            meta = base / "metadata.json"
            metadata = self._metadata(exp_id, timeline=timeline)
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

            # CHECKSUMS
            checksums = self._sha256_dir(base)
            (base / "checksums.sha256").write_text("\n".join(checksums), encoding="utf-8")

            # OPTIONAL: pré-ETL
            if run_pre_etl:
                try:
                    from lab.datasets.pre_etl import generate_conn_features
                    csv_path = generate_conn_features(dataset_root=base)
                    logger.info(f"[HARVEST] Pré-ETL gerado: {csv_path}")
                except Exception as e:
                    logger.error(f"[HARVEST] Pré-ETL falhou (seguindo sem bloquear): {e}")

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
            kernel_victim  = self.ssh.run_command("victim",  "uname -a", timeout=5).strip()
            kernel_sensor  = self.ssh.run_command("sensor",  "uname -a", timeout=5).strip()
            kernel_attacker= self.ssh.run_command("attacker","uname -a", timeout=5).strip()
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
