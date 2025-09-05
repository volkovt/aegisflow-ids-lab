import logging
logger = logging.getLogger("[AttackerAgent]")

class AttackerAgent:
    def __init__(self, ssh):
        self.ssh = ssh  # SSHManager

    def _run(self, host: str, cmd: str, timeout: int = 600) -> str:
        return self.ssh.run_command(host, cmd, timeout=timeout)

    def ensure_apt_ready(self):
        """
        Corrige mirrors/componentes, atualiza keyring e índices do Kali de forma resiliente.
        Replica exatamente o fluxo que funcionou no seu teste manual.
        """
        try:
            logger.info("[Attacker] Preparando APT (sources.list, keyring, índices)...")

            # 1) sources.list canônico com signed-by e non-free-firmware
            self._run("attacker",
                      "sudo sh -lc \""
                      "set -euo pipefail; "
                      "unalias rm 2>/dev/null || true; "
                      "cp /etc/apt/sources.list /etc/apt/sources.list.bak.$(date +%F) || true; "
                      "printf 'deb [signed-by=/usr/share/keyrings/kali-archive-keyring.gpg] "
                      "http://http.kali.org/kali kali-rolling main contrib non-free non-free-firmware\\n' "
                      "> /etc/apt/sources.list\"",
                      timeout=60)

            # 2) limpar índices antigos sem prompt do zsh
            self._run("attacker",
                      "sudo sh -lc \"apt-get clean; command rm -rf /var/lib/apt/lists/*\"",
                      timeout=60)

            # 3) tentativa normal de update + reinstalar keyring (pode falhar na 1ª)
            try:
                self._run("attacker", "sudo DEBIAN_FRONTEND=noninteractive apt-get update -y", timeout=240)
                self._run("attacker", "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --reinstall kali-archive-keyring",
                          timeout=240)
            except Exception as e:
                logger.warning(f"[Attacker] update/keyring (fase normal) falhou: {e}")

            # 4) update permissivo + reinstalar keyring sem autenticação (quebra o ciclo NO_PUBKEY)
            try:
                self._run("attacker",
                          "sudo DEBIAN_FRONTEND=noninteractive apt-get update "
                          "-o Acquire::AllowInsecureRepositories=true -y",
                          timeout=240)
                self._run("attacker",
                          "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --reinstall kali-archive-keyring "
                          "-o APT::Get::AllowUnauthenticated=true",
                          timeout=240)
            except Exception as e:
                logger.warning(f"[Attacker] update/keyring (fase permissiva) falhou: {e}")

            # 5) fallback extra: grava a chave direto via curl+gpg, se disponível
            try:
                self._run("attacker",
                          "sudo sh -lc \""
                          "command -v gpg >/dev/null 2>&1 || true; "  # não tenta instalar gpg (evita loop)
                          "command -v curl >/dev/null 2>&1 && "
                          "curl -fsSL https://archive.kali.org/archive-key.asc | gpg --dearmor "
                          "> /usr/share/keyrings/kali-archive-keyring.gpg || true\"",
                          timeout=120)
            except Exception as e:
                logger.warning(f"[Attacker] fallback curl+gpg falhou (ok prosseguir se a fase acima já resolveu): {e}")

            # 6) update final (verificação normal)
            self._run("attacker", "sudo DEBIAN_FRONTEND=noninteractive apt-get update -y", timeout=240)
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
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {pkg_list}",
                timeout=900
            )

            # Sanidade (mostra versões)
            out = self._run(
                "attacker",
                "sh -lc 'nmap --version | head -1; "
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
