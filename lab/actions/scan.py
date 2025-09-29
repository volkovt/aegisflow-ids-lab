import logging
logger = logging.getLogger("[Action:SCAN]")

class NmapScanAction:
    """
    Scan Nmap com fallback:
      1) tenta -sS (SYN) com flags do workload/params
      2) se falhar por permissão/EPERM, cai p/ -sT -Pn (connect scan)
    """
    def __init__(self, flags=None, ports=None, output="~/exp_nmap/scan", **kwargs):
        self.flags = flags      # ex.: "-sS -sV -O -T3 --open"
        self.ports = ports      # ex.: "1-1024" ou None
        self.output = output    # ex.: "~/exp_nmap/scan"
        self._extra = kwargs

    def _build_flags(self, wl_flags: dict | None) -> str:
        if self.flags:
            return self.flags
        wl = wl_flags or {}
        flags = ["-sS"]
        if bool(wl.get("service_version", True)):
            flags += ["-sV", "-O"]
        timing = str(wl.get("timing", "T3")).upper()
        if timing.startswith("T") and timing[1:].isdigit():
            flags.append(f"-{timing}")
        if wl.get("scripts"):
            flags += ["--script", ",".join(wl["scripts"])]
        if wl.get("top_ports"):
            flags += ["--top-ports", str(int(wl["top_ports"]))]  # opcional
        return " ".join(flags)

    def run(self, attacker_ssh, victim_ip: str, workload=None, **kwargs):
        workload = workload or {}
        nmap_cfg = workload.get("nmap", {}) or {}

        try:
            flags = self._build_flags(nmap_cfg)   # preferir params → workload
            ports_part = f"-p {self.ports} " if self.ports else ""
            out_raw = (self.output or "").strip() or "~/exp_nmap/scan"
            out_shell = "${HOME}/" + out_raw[2:] if out_raw.startswith("~/") else out_raw

            # 1) tenta -sS
            script_sS = (
                "bash -se <<'EOSH'\n"
                "set -euxo pipefail\n"
                f"OUT_PATH=\"{out_shell}\"\n"
                "mkdir -p \"$(dirname \"$OUT_PATH\")\"\n"
                f"sudo nmap {flags} {ports_part}{victim_ip} -oN \"$OUT_PATH\" 2>\"$OUT_PATH.err\" || echo RC=$? >>\"$OUT_PATH.err\"\n"
                "EOSH\n"
            )
            logger.info(f"[SCAN] Nmap tenta -sS em {victim_ip} com: {flags} {ports_part}".strip())
            out = attacker_ssh.run_command("attacker", script_sS, timeout=900)

            # le o .err para decidir fallback
            check_err = (
                "bash -lc 'ERR=\"$(cat "
                f"{out_shell}.err"
                " 2>/dev/null || true)\"; echo \"${ERR}\"'"
            )
            err_txt = attacker_ssh.run_command("attacker", check_err, timeout=10)

            need_fallback = False
            if "Operation not permitted" in (err_txt or ""):
                need_fallback = True
            if "RC=" in (err_txt or "") and any(tag in err_txt for tag in ("1", "2", "RC=1", "RC=2")):
                need_fallback = True

            if need_fallback:
                # 2) fallback -sT -Pn
                flags_t = f"-sT -Pn {(' '.join([f for f in flags.split() if f not in ['-sS']]))}".strip()
                script_sT = (
                    "bash -se <<'EOSH'\n"
                    "set -euxo pipefail\n"
                    f"OUT_PATH=\"{out_shell}.tcp\"\n"
                    "mkdir -p \"$(dirname \"$OUT_PATH\")\"\n"
                    f"sudo nmap {flags_t} {ports_part}{victim_ip} -oN \"$OUT_PATH\" 2>\"$OUT_PATH.err\" || true\n"
                    "EOSH\n"
                )
                logger.warn(f"[SCAN] Fallback p/ -sT -Pn em {victim_ip} (capturado EPERM/erro no -sS).")
                attacker_ssh.run_command("attacker", script_sT, timeout=900)

        except Exception as e:
            logger.error(f"[SCAN] Falhou: {e}")
            raise
