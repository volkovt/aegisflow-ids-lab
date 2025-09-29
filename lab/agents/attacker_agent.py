import logging
logger = logging.getLogger("[AttackerAgent]")

class AttackerAgent:
    def __init__(self, ssh):
        self.ssh = ssh  # SSHManager

    def _run(self, host: str, cmd: str, timeout: int = 600) -> str:
        return self.ssh.run_command(host, cmd, timeout=timeout)

    def ensure_apt_ready(self):
        """
        Torna o APT do Kali funcional mesmo com rotação de chaves:
        - Usa [trusted=yes] TEMPORÁRIO só para baixar a chave oficial.
        - Instala ca-certificates/gnupg/curl.
        - Grava /usr/share/keyrings/kali-archive-keyring.gpg.
        - Restaura sources com [signed-by=...] e valida com update normal.
        """
        try:
            logger.info("[Attacker] Reparando APT/keyring (Kali)…")

            # Sources temporário com bypass (somente para instalar a chave)
            self._run(
                "attacker",
                "sudo bash -lc \"set -euo pipefail; "
                "cp /etc/apt/sources.list /etc/apt/sources.list.bak.$(date +%F) || true; "
                "printf 'deb [trusted=yes] https://http.kali.org/kali kali-rolling main contrib non-free non-free-firmware\\n' "
                "> /etc/apt/sources.list; "
                "apt-get clean; rm -rf /var/lib/apt/lists/* || true; "
                "apt-get update -o Acquire::AllowInsecureRepositories=true\"",
                timeout=240
            )

            # Dependências para obter/instalar a chave
            self._run(
                "attacker",
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
                "ca-certificates gnupg curl",
                timeout=480
            )

            # Grava o keyring oficial
            self._run(
                "attacker",
                "sudo bash -lc \"set -e; "
                "curl -fsSL https://archive.kali.org/archive-key.asc | "
                "gpg --dearmor > /usr/share/keyrings/kali-archive-keyring.gpg\"",
                timeout=180
            )

            # Restaura sources com signed-by e valida
            self._run(
                "attacker",
                "sudo bash -lc \"set -e; "
                "printf 'deb [signed-by=/usr/share/keyrings/kali-archive-keyring.gpg] "
                "https://http.kali.org/kali kali-rolling main contrib non-free non-free-firmware\\n' "
                "> /etc/apt/sources.list; "
                "apt-get clean; rm -rf /var/lib/apt/lists/* || true\"",
                timeout=120
            )
            self._run("attacker", "sudo DEBIAN_FRONTEND=noninteractive apt-get update -y", timeout=480)

            logger.info("[Attacker] APT pronto (keyring atualizado e índices válidos).")
        except Exception as e:
            logger.error(f"[Attacker] Falha em ensure_apt_ready: {e}")
            raise

    def ensure_tools(self, extra_tools=None):
        """
        Instala ferramentas necessárias ao experimento no atacante.
        """
        try:
            self.ensure_apt_ready()

            base_tools = ["nmap", "hydra", "slowhttptest", "curl", "jq"]
            if extra_tools:
                base_tools.extend(extra_tools)

            pkg_list = " ".join(sorted(set(base_tools)))
            logger.info(f"[Attacker] Instalando ferramentas: {pkg_list} ...")

            self._run(
                "attacker",
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends " + pkg_list,
                timeout=900
            )

            # Sanidade (mostra versões)
            out = self._run(
                "attacker",
                # TROCA: sh -> bash (por consistência)
                "bash -lc 'nmap --version | head -1; "
                "hydra -h | head -1; "
                "slowhttptest -h | head -1 || true; "
                "curl --version | head -1; "
                "jq --version'",
                timeout=60
            )
            logger.info(f"[Attacker] Ferramentas OK:\n{out}")
        except Exception as e:
            logger.error(f"[Attacker] Erro instalando ferramentas: {e}")
            raise

    # em lab/agents/attacker_agent.py
    def start_benign_burst(self, benign_cfg, duration_s=15, cancel_event=None):
        """
        Gera tráfego benigno curto (curl, ping, iperf3) conforme benign_cfg do exp.
        Não bloqueia o pipeline além do tempo previsto.
        """
        if not benign_cfg:
            return
        try:
            urls = (benign_cfg.get("http", {}) or {}).get("urls", [])
            for u in urls:
                if cancel_event and cancel_event.is_set():
                    break
                cmd = f"bash -lc \"curl -m 3 -s -o /dev/null '{u}' || true\""
                self.ssh.run_command("attacker", cmd, timeout=5)
            # pequeno ping
            ping_cnt = int((benign_cfg.get("icmp", {}) or {}).get("ping_count", 5))
            icmp_target = benign_cfg.get('icmp_target', '192.168.56.10')  # por padrão, pinga o sensor
            self.ssh.run_command(
                "attacker",
                f"bash -lc \"ping -c {ping_cnt} {icmp_target} || true\"",
                timeout=10
            )
            # iperf3 (se habilitado)
            iperf = benign_cfg.get("iperf3", {}) or {}
            if iperf.get("enabled"):
                dur = int(iperf.get("duration_s", 5))
                srv = iperf.get("server")
                rev = "--reverse" if iperf.get("reverse") else ""
                self.ssh.run_command("attacker", f"bash -lc \"iperf3 -c {srv} -t {dur} {rev} || true\"", timeout=dur + 5)
        except Exception as e:
            logger = logging.getLogger("[AttackerAgent]")
            logger.warning(f"[AttackerAgent] benign burst: {e}")
