import logging
logger = logging.getLogger("[AttackerAgent]")

class AttackerAgent:
    def __init__(self, ssh):
        self.ssh = ssh

    def ensure_tools(self):
        try:
            logger.info("[Attacker] Instalando nmap/hydra/slowhttptest/hping3...")
            self.ssh.run_command("attacker", "sudo apt-get update -y", timeout=120)
            self.ssh.run_command("attacker", "sudo apt-get install -y nmap hydra slowhttptest hping3", timeout=300)
        except Exception as e:
            logger.error(f"[Attacker] Erro instalando ferramentas: {e}")
            raise
