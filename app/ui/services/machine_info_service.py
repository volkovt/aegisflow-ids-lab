from __future__ import annotations
from typing import Callable, Tuple
import logging

from PySide6.QtCore import QTimer


class MachineInfoService:
    """Coleta e normaliza informações de SO/Host/IP das VMs."""

    def __init__(self, *, vagrant, ssh, cfg, lab_dir, append_log: Callable[[str], None], logger: logging.Logger):
        self.vagrant = vagrant
        self.ssh = ssh
        self.cfg = cfg
        self.lab_dir = lab_dir
        self.append_log = append_log
        self.logger = logger

    # -------- helpers --------
    def infer_os_from_box(self, box: str) -> str:
        try:
            b = (box or "").lower()
            if "kali" in b:
                return "Kali Linux"
            if "ubuntu" in b:
                return "Ubuntu"
            if "debian" in b:
                return "Debian"
            if any(x in b for x in ("centos", "rocky", "almalinux")):
                return "RHEL-like"
            if "windows" in b or "win" in b:
                return "Windows"
            return box or "desconhecido"
        except Exception as e:
            self.append_log(f"[WARN] infer_os_from_box: {e}")
            return "desconhecido"

    def query_os_friendly(self, name: str, timeout: int = 12) -> str:
        cmd_linux = (
            "set -e\n"
            "get_name() {\n"
            "  if command -v lsb_release >/dev/null 2>&1; then lsb_release -ds && printf ' (%s)\\n' \"$(lsb_release -cs)\"; return; fi\n"
            "  if [ -r /etc/os-release ]; then . /etc/os-release; printf '%s\\n' \"${PRETTY_NAME:-$NAME $VERSION}\"; return; fi\n"
            "  if [ -r /etc/redhat-release ]; then cat /etc/redhat-release; return; fi\n"
            "  if [ -r /etc/debian_version ]; then printf 'Debian %s\\n' \"$(cat /etc/debian_version)\"; return; fi\n"
            "  uname -sr\n"
            "}\n"
            "NAME=\"$(get_name || true)\"\n"
            "ARCH=\"$(uname -m || echo '?')\"\n"
            "KERN=\"$(uname -r || echo '?')\"\n"
            "NAME=\"${NAME#\"}\"; NAME=\"${NAME%\"}\"\n"
            "printf '%s (%s, kernel %s)\\n' \"$NAME\" \"$ARCH\" \"$KERN\"\n"
        )
        try:
            out = self.ssh.run_command(name, cmd_linux, timeout=timeout).strip()
            if out:
                self.append_log(f"[SO] {name}: {out}")
                return out
        except Exception as e:
            self.append_log(f"[WARN] coleta SO (Linux) falhou em {name}: {e}")

        ps = (
            r'powershell -NoProfile -Command '
            r'"$o=Get-CimInstance Win32_OperatingSystem; '
            r'Write-Output ($o.Caption + \" \" + $o.Version + \" (\" + $o.OSArchitecture + ", build " + $o.BuildNumber + ")\")"'
        )
        try:
            outw = self.ssh.run_command(name, ps, timeout=timeout).strip()
            if outw:
                self.append_log(f"[SO] {name}: {outw}")
                return outw
        except Exception as e:
            self.append_log(f"[WARN] coleta SO (Windows) falhou em {name}: {e}")

        try:
            out2 = self.ssh.run_command(name, "uname -sr", timeout=8).strip()
            if out2:
                self.append_log(f"[SO] {name} (fallback): {out2}")
                return out2
        except Exception as e:
            self.append_log(f"[WARN] coleta SO fallback (uname) falhou em {name}: {e}")
        return "SO desconhecido"

    # -------- public --------
    def collect_machine_details(self, name: str, *, state_hint: str | None = None) -> Tuple[str, str, str]:
        try:
            m = {x.name: x for x in self.cfg.machines}[name]
        except KeyError:
            self.append_log(f"[WARN] VM '{name}' não encontrada no config.")
            return ("desconhecido", "—", "—")

        guest_ip = f"{self.cfg.ip_base}{m.ip_last_octet}"
        os_text = self.infer_os_from_box(m.box)

        try:
            state = state_hint if state_hint is not None else self.vagrant.status_by_name(name)
            if state == "running":
                try:
                    self.vagrant.wait_ssh_ready(name, str(self.lab_dir), attempts=10, delay_s=3)
                except Exception as e:
                    self.append_log(f"[WARN] wait_ssh_ready falhou em {name}: {e}")
                try:
                    f = self.ssh.get_ssh_fields_safe(name)
                    host_endpoint = f"{f.get('HostName', '?')}:{f.get('Port', '?')}"
                except Exception as e:
                    self.append_log(f"[WARN] ssh-config falhou em {name}: {e}")
                    host_endpoint = "—"

                try:
                    os_text = self.query_os_friendly(name, timeout=12)
                except Exception as e:
                    self.append_log(f"[WARN] query_os_friendly falhou em {name}: {e}")
                return (os_text, host_endpoint, guest_ip)
            else:
                return (os_text, "—", guest_ip)
        except Exception as e:
            self.append_log(f"[WARN] collect_machine_details erro em {name}: {e}")
            return (os_text, "—", guest_ip)
