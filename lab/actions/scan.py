import logging
logger = logging.getLogger("[Action:SCAN]")

class NmapScanAction:
    def __init__(self, output="exp_scan.nmap"):
        self.output = output

    def run(self, attacker_ssh, victim_ip: str):
        try:
            cmd = f"IP={victim_ip}; sudo nmap -sS -sV -O -T4 $IP -oN ~/{self.output}"
            logger.info(f"[SCAN] Executando: {cmd}")
            attacker_ssh.run_command("attacker", cmd, timeout=600)
        except Exception as e:
            logger.error(f"[SCAN] Falhou: {e}")
            raise
