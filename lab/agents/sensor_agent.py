import time
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
        Instala tcpdump e tenta Zeek quando disponível no repositório.
        Se Zeek não existir (Candidate: none), segue PCAP-only sem quebrar o fluxo.
        """
        try:
            logger.info("[Sensor] Instalando tcpdump e avaliando Zeek...")
            self.ssh.run("sensor", "sudo DEBIAN_FRONTEND=noninteractive apt-get update -y", timeout_s=240)
            self.ssh.run("sensor", "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y tcpdump", timeout_s=300)

            # Verifica se há 'zeek' disponível no repo
            try:
                cand = self.ssh.run(
                    "sensor",
                    "bash -lc \"apt-cache policy zeek | awk '/Candidate:/ {print $2}'\"",
                    timeout_s=15
                )
                cand = (cand or "").strip()
            except Exception:
                cand = ""

            if cand and cand != "(none)":
                try:
                    self.ssh.run(
                        "sensor",
                        "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y zeek",
                        timeout_s=600
                    )
                    self.ssh.run("sensor", "bash -lc 'command -v zeek'", timeout_s=10)
                    self.zeek_ok = True
                    logger.info("[Sensor] Zeek instalado e disponível.")
                except Exception as e:
                    self.zeek_ok = False
                    logger.warning(f"[Sensor] Zeek indisponível ({e}) — seguiremos apenas com PCAP.")
            else:
                self.zeek_ok = False
                logger.info("[Sensor] Repositório sem 'zeek' (Candidate: none). Seguindo com PCAP. "
                            "Dica: habilite o repo oficial do Zeek se quiser logs em tempo real.")
        except Exception as e:
            logger.error(f"[Sensor] Erro instalando ferramentas: {e}")
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

    def arm_capture(self, rotate_sec=300, rotate_mb=100, zeek_rotate_sec=3600):
        """
        Arma captura com rotação de PCAP e, se disponível, Zeek em tempo real.
        - PCAP: /var/log/pcap/exp_%Y%m%d_%H%M%S.pcap (-G rotate_sec, -C rotate_mb)
        - ZEEK: /var/log/zeek (Log::default_rotation_interval=zeek_rotate_sec)
        """
        iface = self._iface()
        try:
            logger.info(f"[Sensor] Ligando captura em {iface} (tcpdump com rotação)...")
            self.ssh.run("sensor", "sudo mkdir -p /var/log/pcap /var/log/zeek", timeout_s=10)

            # Inicia tcpdump e grava PID de forma correta
            tcpdump_cmd = f"tcpdump -i {iface} -w /var/log/pcap/exp_%Y%m%d_%H%M%S.pcap -G {rotate_sec} -C {rotate_mb} -n"
            self._spawn_with_pidfile("sensor", tcpdump_cmd, "/var/run/tcc_tcpdump.pid", timeout_s=10)

            # Grace period para primeiro arquivo aparecer
            time.sleep(3)

            if self.zeek_ok:
                zeek_cmd = f"bash -lc 'export ZEEK_LOG_DIR=/var/log/zeek; zeek -i {iface} Log::default_rotation_interval={zeek_rotate_sec}'"
                # Observação: o 'bash -lc' acima faz parte do comando do Zeek (para export do env).
                # Aqui embrulhamos em um 'sh -c' para manter o mesmo padrão de nohup + $! correto.
                self._spawn_with_pidfile(
                    "sensor",
                    f"sh -c {repr(zeek_cmd)}",
                    "/var/run/tcc_zeek.pid",
                    timeout_s=10
                )
                logger.info("[Sensor] Zeek em tempo real armado.")
            else:
                logger.info("[Sensor] Zeek ausente — apenas PCAP ativo.")
        except Exception as e:
            logger.error(f"[Sensor] Erro ao armar captura: {e}")
            raise

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
