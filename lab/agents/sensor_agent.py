import logging

logger = logging.getLogger("[SensorAgent]")


class SensorAgent:
    """
    Agente do Sensor:
    - Garante ferramentas mínimas (tcpdump; Zeek se disponível).
    - Arma/desarma captura com rotação (PCAP) e, opcionalmente, Zeek em tempo real.
    - Health-check de artefatos e processos.
    """
    def __init__(self, ssh):
        self.ssh = self._wrap_ssh(ssh)
        self.zeek_ok = False

    # ---------------------------
    # Infra helpers
    # ---------------------------

    def _wrap_ssh(self, ssh):
        """
        Adapta a interface de SSH para tolerar ausência de 'run_command_cancellable',
        caindo para 'run_command' quando necessário.
        """
        class _SSH:
            def __init__(self, inner):
                self.inner = inner

            def run(self, host: str, cmd: str, timeout_s: int = 20):
                try:
                    if hasattr(self.inner, "run_command_cancellable"):
                        return self.inner.run_command_cancellable(host, cmd, timeout_s=timeout_s)
                    return self.inner.run_command(host, cmd, timeout=timeout_s)
                except TypeError:
                    # Compat: alguns chamadores legados usam 'timeout' em vez de 'timeout_s'
                    if hasattr(self.inner, "run_command_cancellable"):
                        return self.inner.run_command_cancellable(host, cmd, timeout_s=timeout_s)
                    return self.inner.run_command(host, cmd, timeout=timeout_s)

            def run_basic(self, host: str, cmd: str, timeout: int = 20):
                # Acesso "direto" quando o chamador quer timeout=int
                if hasattr(self.inner, "run_command"):
                    return self.inner.run_command(host, cmd, timeout=timeout)
                # Fallback tenta cancellable
                return self.inner.run_command_cancellable(host, cmd, timeout_s=timeout)

        return _SSH(ssh)

    def _iface(self) -> str:
        """
        Detecta a interface do host-only 192.168.56.0/24 (VirtualBox),
        com fallback padrão 'enp0s8'.
        """
        try:
            cmd = r"bash -lc \"ip -br addr | awk '/192\.168\.56\./{print $1; exit}'\""
            out = self.ssh.run("sensor", cmd, timeout_s=10)
            out = (out or "").strip()
            return out or "enp0s8"
        except Exception as e:
            logger.error(f"[Sensor] Falha ao detectar interface: {e}")
            return "enp0s8"

    # ---------------------------
    # Provisionamento
    # ---------------------------

    def ensure_tools(self):
        """
        Instala tcpdump/jq/curl e tenta Zeek quando houver Candidate.
        Se Zeek estiver fora do PATH (ex.: /opt/zeek/bin/zeek), ajusta PATH e cria symlink.
        """
        try:
            logger.info("[Sensor] Instalando tcpdump/jq/curl e avaliando Zeek...")
            self.ssh.run("sensor", "sudo DEBIAN_FRONTEND=noninteractive apt-get update -y", timeout_s=240)
            self.ssh.run(
                "sensor",
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y tcpdump jq curl",
                timeout_s=300
            )

            # Verifica Candidate do zeek e tenta instalar por APT
            try:
                cand = self.ssh.run(
                    "sensor",
                    r"bash -lc ""apt-cache policy zeek | awk '/Candidate:/ {print $2}'""",
                    timeout_s=10
                ).strip()
            except Exception:
                cand = ""

            if cand and cand != "(none)":
                try:
                    self.ssh.run(
                        "sensor",
                        "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y zeek",
                        timeout_s=600
                    )
                except Exception as e:
                    logger.warning(f"[Sensor] Falha instalando Zeek por APT (seguindo): {e}")

            # Fallback: Zeek do OBS em /opt/zeek/bin (ajusta PATH e symlink)
            self.ssh.run(
                "sensor",
                r"bash -lc 'if [ -x /opt/zeek/bin/zeek ]; then "
                r"echo export PATH=/opt/zeek/bin:\$PATH | sudo tee /etc/profile.d/zeek.sh >/dev/null; "
                r"sudo chmod +x /etc/profile.d/zeek.sh; "
                r"sudo ln -sf /opt/zeek/bin/zeek /usr/local/bin/zeek || true; "
                r"hash -r || true; "
                r"fi'",
                timeout_s=20
            )

            # Sinaliza disponibilidade (PATH ou /opt)
            z = self.ssh.run(
                "sensor",
                "bash -lc 'command -v zeek || { [ -x /opt/zeek/bin/zeek ] && echo /opt/zeek/bin/zeek; }'",
                timeout_s=10
            ).strip()
            self.zeek_ok = bool(z)
            if self.zeek_ok:
                logger.info(f"[Sensor] Zeek disponível: {z}")
            else:
                logger.info("[Sensor] Zeek ausente — seguiremos PCAP-only.")

            # Pastas do pipeline
            self.ssh.run("sensor", "sudo mkdir -p /var/log/pcap /var/log/zeek", timeout_s=10)

        except Exception as e:
            logger.error(f"[Sensor] Erro instalando ferramentas: {e}")
            raise

    def arm_capture(self, rotate_sec: int = 300, rotate_mb: int = 100, zeek_rotate_sec: int = 3600):
        """
        Arma captura com rotação (tcpdump) e, se disponível, Zeek em tempo real.
        """
        iface = self._iface()
        try:
            logger.info(f"[Sensor] Ligando captura (iface={iface})...")
            self.ssh.run(
                "sensor",
                f"nohup bash -lc \"tcpdump -i {iface} -w /var/log/pcap/exp_%Y%m%d_%H%M%S.pcap -G {rotate_sec} -C {rotate_mb} -n\" >/dev/null 2>&1 &",
                timeout_s=8
            )

            # pequeno grace-period
            try:
                import time as _t
                _t.sleep(3)
            except Exception:
                pass

            if self.zeek_ok:
                zeek_cmd = (
                    "bash -lc 'export ZEEK_LOG_DIR=/var/log/zeek; "
                    "ZEEKBIN=$(command -v zeek || echo /opt/zeek/bin/zeek); "
                    f"\"$ZEEKBIN\" -i {iface} Log::default_rotation_interval={zeek_rotate_sec}'"
                )
                self.ssh.run(
                    "sensor",
                    f"nohup {zeek_cmd} >/dev/null 2>&1 &",
                    timeout_s=8
                )
                logger.info("[Sensor] Zeek em tempo real armado.")
            else:
                logger.info("[Sensor] Zeek ausente — apenas PCAP ativo.")
        except Exception as e:
            logger.error(f"[Sensor] Erro ao armar captura: {e}")
            raise

    # ---------------------------
    # Captura
    # ---------------------------

    def _spawn_with_pidfile(self, host: str, start_cmd: str, pidfile: str, timeout_s: int = 8):
        """
        Inicia um processo em background e grava o PID correto no pidfile,
        garantindo que '$!' seja o PID do próprio processo (e não do shell/nohup).
        """
        sh = (
            "bash -lc "
            f"\"set -e; sudo mkdir -p /var/run; sudo rm -f {pidfile}; "
            f"nohup {start_cmd} >/dev/null 2>&1 & echo $! | sudo tee {pidfile} >/dev/null\""
        )
        return self.ssh.run(host, sh, timeout_s=timeout_s)

    def stop_capture(self):
        """
        Para captura com base nos pidfiles; se não houver, faz pkill por padrão.
        Tolera ausência de API 'cancellable'.
        """
        cmd = (
            "bash -lc \"for p in /var/run/tcc_*.pid; do "
            "[ -f $p ] && sudo kill $(cat $p) 2>/dev/null || true; done; "
            "sudo pkill -f 'tcpdump -i' || true; sudo pkill -f 'zeek -i' || true\""
        )
        try:
            self.ssh.run("sensor", cmd, timeout_s=20)
            logger.info("[Sensor] Captura parada.")
        except Exception as e:
            logger.warning(f"[Sensor] Falha ao parar captura (seguindo): {e}")

    # ---------------------------
    # Saúde/Diagnóstico
    # ---------------------------

    def health(self) -> bool:
        """
        Retorna True se há artefatos recentes ou processo de captura ativo.
        """
        try:
            if self.zeek_ok:
                out = self.ssh.run("sensor", "bash -lc 'ls -1 /var/log/zeek 2>/dev/null | tail -n 3'", timeout_s=10)
                if (out or "").strip():
                    return True
            else:
                out = self.ssh.run("sensor", "bash -lc 'ls -1 /var/log/pcap/*.pcap 2>/dev/null | tail -n 3'", timeout_s=10)
                if (out or "").strip():
                    return True

            procs = self.ssh.run(
                "sensor",
                "bash -lc \"pgrep -fa 'tcpdump -i' | tail -n 1\"",
                timeout_s=6
            )
            ok = bool((procs or "").strip())
            if not ok:
                logger.warning("[Sensor] Nenhum PCAP/Zeek recente e tcpdump não encontrado — verifique permissões.")
            return ok
        except Exception as e:
            logger.warning(f"[Sensor] Health-check falhou: {e}")
            return False
