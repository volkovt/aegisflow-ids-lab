import logging, json, hashlib, shutil, os, subprocess
from pathlib import Path

logger = logging.getLogger("[DataCollector]")

class DataCollector:
    def __init__(self, ssh, lab_dir: Path):
        self.ssh = ssh
        self.lab_dir = Path(lab_dir)

    def _scp(self, host: str, remote: str, local: Path):
        try:
            fields = self.ssh.get_ssh_fields(host)
            local_parent = local if str(local).endswith("/") or str(local).endswith("\\") else local.parent
            local_parent.mkdir(parents=True, exist_ok=True)
            key = fields["IdentityFile"]
            port = fields["Port"]
            user = fields["User"]
            hostn = fields["HostName"]
            cmd = ["scp","-P",str(port),"-i",key,f"{user}@{hostn}:{remote}",str(local)]
            logger.info(f"[SCP] {' '.join(cmd)}")
            subprocess.check_call(cmd, cwd=self.lab_dir)
        except Exception as e:
            logger.error(f"[SCP] Falhou: {e}")
            raise

    def harvest(self, exp_id: str, out_base: Path, timeline=None, run_pre_etl: bool = True) -> Path:
        base = Path(out_base) / exp_id
        sensor_pcap = base / "sensor" / "pcap" / ""
        sensor_zeek = base / "sensor" / "zeek" / ""
        victim_dir  = base / "victim"
        attacker_dir= base / "attacker" / ""

        try:
            # SENSOR
            self._scp("sensor", "/var/log/zeek/*.log", sensor_zeek)
            self._scp("sensor", "/var/log/pcap/*.pcap", sensor_pcap)

            # VICTIM
            self._scp("victim", "/var/log/auth.log", victim_dir / "auth.log")

            # ATTACKER (artefatos de ações)
            self._scp("attacker", "~/exp_scan.nmap", attacker_dir)
            self._scp("attacker", "~/exp_brute.hydra", attacker_dir)
            self._scp("attacker", "~/exp_dos*", attacker_dir)

            # METADATA
            meta = base / "metadata.json"
            metadata = self._metadata(exp_id, timeline=timeline)
            meta.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

            # CHECKSUMS
            checksums = self._sha256_dir(base)
            (base / "checksums.sha256").write_text("\n".join(checksums), encoding="utf-8")

            # OPTIONAL: pré-ETL
            if run_pre_etl:
                try:
                    from datasets.pre_etl import generate_conn_features
                    csv_path = generate_conn_features(dataset_root=base)
                    logger.info(f"[HARVEST] Pré-ETL gerado: {csv_path}")
                except Exception as e:
                    logger.warn(f"[HARVEST] Pré-ETL falhou (seguindo sem bloquear): {e}")

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
                with open(fp, "rb") as r:
                    for chunk in iter(lambda: r.read(1024*1024), b""):
                        h.update(chunk)
                out.append(f"{h.hexdigest()}  {fp.relative_to(path)}")
        return out

    def _metadata(self, exp_id: str, timeline=None) -> dict:
        try:
            kernel_victim = self.ssh.run_command("victim", "uname -a", timeout=5).strip()
            kernel_sensor = self.ssh.run_command("sensor", "uname -a", timeout=5).strip()
            kernel_attacker= self.ssh.run_command("attacker","uname -a", timeout=5).strip()
        except Exception as e:
            logger.warn(f"[META] Falha ao coletar uname: {e}")
            kernel_victim = kernel_sensor = kernel_attacker = "unknown"
        return {
            "exp_id": exp_id,
            "timeline": timeline or {},
            "guests": {
                "victim": {"uname": kernel_victim},
                "sensor": {"uname": kernel_sensor},
                "attacker": {"uname": kernel_attacker}
            },
            "tools": {
                "sensor": ["tcpdump","zeek"],
                "attacker": ["nmap","hydra","slowhttptest","hping3"]
            }
        }
