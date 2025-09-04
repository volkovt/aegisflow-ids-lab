import logging
logger = logging.getLogger("[SensorAgent]")

class SensorAgent:
    def __init__(self, ssh):
        self.ssh = ssh  # SSHManager

    def _iface(self) -> str:
        try:
            cmd = r"ip -br addr | awk '/192\.168\.56\./{print $1; exit}'"
            out = self.ssh.run_command("sensor", cmd, timeout=10).strip()
            return out or "enp0s8"
        except Exception as e:
            logger.error(f"[Sensor] Falha ao detectar interface: {e}")
            return "enp0s8"

    def ensure_tools(self):
        try:
            logger.info("[Sensor] Instalando tcpdump/zeek se necessário...")
            self.ssh.run_command("sensor", "sudo apt-get update -y", timeout=120)
            self.ssh.run_command("sensor", "sudo apt-get install -y tcpdump zeek", timeout=300)
        except Exception as e:
            logger.error(f"[Sensor] Erro instalando ferramentas: {e}")
            raise

    def arm_capture(self, rotate_sec=300, rotate_mb=100, zeek_rotate_sec=3600):
        iface = self._iface()
        try:
            logger.info(f"[Sensor] Ligando captura em {iface}...")
            self.ssh.run_command("sensor", "sudo mkdir -p /var/log/pcap /var/log/zeek", timeout=10)

            tcpdump = (
                f"nohup bash -lc 'tcpdump -i {iface} -w /var/log/pcap/exp_%Y%m%d_%H%M%S.pcap "
                f"-G {rotate_sec} -C {rotate_mb} -n' >/dev/null 2>&1 &"
            )
            self.ssh.run_command("sensor", tcpdump, timeout=8)

            zeek = (
                f"nohup bash -lc 'export ZEEK_LOG_DIR=/var/log/zeek; "
                f"zeek -i {iface} Log::default_rotation_interval={zeek_rotate_sec}' >/dev/null 2>&1 &"
            )
            self.ssh.run_command("sensor", zeek, timeout=8)
            logger.info("[Sensor] Captura armada.")
        except Exception as e:
            logger.error(f"[Sensor] Erro ao armar captura: {e}")
            raise

    def health(self) -> bool:
        try:
            out = self.ssh.run_command("sensor", "ls -1 /var/log/zeek | tail -n 3", timeout=10)
            ok = bool(out.strip())
            if not ok:
                logger.warning("[Sensor] Nenhum arquivo Zeek recente detectado.")
            return ok
        except Exception as e:
            logger.warning(f"[Sensor] Health-check falhou: {e}")
            return False
