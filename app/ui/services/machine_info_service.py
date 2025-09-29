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
        """
        Coleta amigável do SO:
        - Apenas comandos POSIX (Linux) sem ${...} (format-safe).
        - Se falhar, tenta PowerShell (Windows) como último recurso.
        - Fallback final: uname -sr.
        """
        # Linux: sem ${…} nem funções; tudo linear e “format-safe”
        cmd_linux = (
            "set -e\n"
            # 1) Tenta lsb_release (rápido e padronizado)
            "name=\"\"; code=\"\"; arch=\"?\"; kern=\"?\"\n"
            "if command -v uname >/dev/null 2>&1; then arch=\"$(uname -m 2>/dev/null || echo '?')\"; kern=\"$(uname -r 2>/dev/null || echo '?')\"; fi\n"
            "if command -v lsb_release >/dev/null 2>&1; then\n"
            "  name=\"$(lsb_release -ds 2>/dev/null || true)\"; code=\"$(lsb_release -cs 2>/dev/null || true)\";\n"
            "  if [ -n \"$code\" ]; then name=\"$name ($code)\"; fi\n"
            "fi\n"
            # 2) /etc/os-release via awk, sem ${…}
            "if [ -z \"$name\" ] && [ -r /etc/os-release ]; then\n"
            "  name=\"$(awk -F= '\n"
            "    /^PRETTY_NAME=/{ gsub(/^\"|\"$/, \"\", $2); print $2; found=1; exit }\n"
            "    END{ if(!found){ n=\"\"; v=\"\" } }\n"
            "  ' /etc/os-release 2>/dev/null || true)\"\n"
            "  if [ -z \"$name\" ]; then\n"
            "    name=\"$(awk -F= '\n"
            "      /^NAME=/{ n=$2 }\n"
            "      /^VERSION=/{ v=$2 }\n"
            "      END{\n"
            "        gsub(/^\"|\"$/, \"\", n); gsub(/^\"|\"$/, \"\", v);\n"
            "        if(n!=\"\"){ printf(\"%s %s\\n\", n, v) }\n"
            "      }\n"
            "    ' /etc/os-release 2>/dev/null || true)\"\n"
            "  fi\n"
            "fi\n"
            # 3) Demais distros
            "if [ -z \"$name\" ] && [ -r /etc/redhat-release ]; then name=\"$(cat /etc/redhat-release)\"; fi\n"
            "if [ -z \"$name\" ] && [ -r /etc/debian_version ]; then name=\"Debian $(cat /etc/debian_version)\"; fi\n"
            "if [ -z \"$name\" ]; then name=\"$(uname -sr 2>/dev/null || echo Linux)\"; fi\n"
            "printf '%s (%s, kernel %s)\\n' \"$name\" \"$arch\" \"$kern\"\n"
        )

        try:
            out = self.ssh.run_command(name, cmd_linux, timeout=timeout).strip()
            if out:
                self.append_log(f"[SO] {name}: {out}")
                return out
        except Exception as e:
            self.append_log(f"[WARN] coleta SO (Linux) falhou em {name}: {e}")

            # Windows (só tenta se Linux falhar)
            ps = (
                r"powershell -NoProfile -Command "
                r"\"$o=Get-CimInstance Win32_OperatingSystem; "
                r"Write-Output ($o.Caption + ' ' + $o.Version + ' (' + $o.OSArchitecture + ', build ' + $o.BuildNumber + ')')\""
            )
        try:
            outw = self.ssh.run_command(name, ps, timeout=timeout).strip()
            if outw:
                self.append_log(f"[SO] {name}: {outw}")
                return outw
        except Exception as e:
            self.append_log(f"[WARN] coleta SO (Windows) falhou em {name}: {e}")

            # Fallback universal
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
