import logging
import base64

logger = logging.getLogger("[SensorAgent]")

class SensorAgent:
    def __init__(self, ssh_manager):
        self.ssh = ssh_manager

    def _run_b64(self, machine: str, script: str, timeout: int = 300, require_root: bool = True):
        b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        try:
            if require_root:
                cmd = f'sudo -n bash -lc "echo {b64} | base64 -d | bash"'
                self.ssh.run_command(machine, cmd, timeout=timeout)
            else:
                cmd = f'bash -lc "echo {b64} | base64 -d | bash"'
                self.ssh.run_command(machine, cmd, timeout=timeout)
            logger.info(f"[SensorAgent] Script (b64) executado em {machine}.")
        except Exception as e:
            logger.warning(f"[SensorAgent] sudo -n falhou em {machine}: {e} — tentando sem sudo.")
            cmd2 = f'bash -lc "echo {b64} | base64 -d | bash"'
            try:
                self.ssh.run_command(machine, cmd2, timeout=timeout)
                logger.info(f"[SensorAgent] Script (b64) executado em {machine} sem sudo.")
            except Exception as e2:
                logger.error(f"[SensorAgent] Falha _run_b64 em {machine}: {e2}")
                raise

    def enable_promisc(self, iface_hint: str = "eth1"):
        """
        Coloca a interface do sensor em modo promíscuo e cria um serviço systemd
        para aplicar a cada boot. Usa iface_hint como padrão.
        """
        script = f"""
            set -e
            IFACE="{iface_hint}"
            # Se a hint não existir, tenta descobrir a iface do lab (a que tem /24 192.168.56.0/24)
            if ! ip link show "$IFACE" >/dev/null 2>&1; then
              IFACE="$(ip -o -4 addr show | awk '/192\\.168\\.56\\./ {{print $2; exit}}' || true)"
            fi
            if [ -z "$IFACE" ]; then
              IFACE="$(ip -br link | awk '/UP/ && !/LOOPBACK/ {{print $1; exit}}')"
            fi
            echo "[promisc] usando IFACE=$IFACE"
            
            ip link set "$IFACE" promisc on
            
            cat >/etc/systemd/system/promisc-{iface_hint}.service <<EOF
            [Unit]
            Description=Enable promiscuous mode on {iface_hint}
            After=network-online.target
            Wants=network-online.target
            
            [Service]
            Type=oneshot
            ExecStart=/sbin/ip link set {iface_hint} promisc on
            RemainAfterExit=yes
            
            [Install]
            WantedBy=multi-user.target
            EOF
            
            systemctl daemon-reload
            systemctl enable --now promisc-{iface_hint}.service || true
            
            ip -br link show "$IFACE" | sed 's/\\s\\+/ /g'
        """
        try:
            self._run_b64("sensor", script, timeout=30, require_root=True)
            logger.info("[SensorAgent] Modo promíscuo ativado no sensor.")
        except Exception as e:
            logger.error(f"[SensorAgent] enable_promisc falhou: {e}")
            raise

    def mirror_smoke_test(self, victim_ip: str, packets: int = 8, iface_hint: str = "eth1"):
        """
        Verificação rápida: tenta ver pacotes TCP/22 para a vítima.
        Retorna True se capturar algo.
        """
        script = f"""
            set -e
            IFACE="{iface_hint}"
            timeout 6 tcpdump -ni "$IFACE" "tcp and dst host {victim_ip} and dst port 22" -c {packets} >/tmp/mirror_test.out 2>&1 || true
            LINES=$(wc -l </tmp/mirror_test.out || echo 0)
            echo "[mirror] linhas=$LINES"
            tail -n 5 /tmp/mirror_test.out || true
            test "$LINES" -gt 0 && exit 0 || exit 1
        """
        try:
            self._run_b64("sensor", script, timeout=15, require_root=True)
            logger.info("[SensorAgent] mirror_smoke_test OK: tráfego espelhado alcançado.")
            return True
        except Exception as e:
            logger.warn(f"[SensorAgent] mirror_smoke_test falhou (sem tráfego?): {e}")
            return False

    def ensure_tools(self):
        """
        Instala Zeek/tcpdump e utilitários no sensor (idempotente).
        """
        script = r"""
            set -e
            export DEBIAN_FRONTEND=noninteractive
            apt_opts="-o Dpkg::Lock::Timeout=60 -o Acquire::Retries=3"
            
            apt-get $apt_opts update || true
            apt-get $apt_opts install -y tcpdump jq curl gnupg ca-certificates apt-transport-https tmux iproute2 net-tools tshark || true
            
            if ! command -v zeek >/dev/null 2>&1 && [ ! -x /opt/zeek/bin/zeek ]; then
              echo deb http://download.opensuse.org/repositories/security:/zeek/xUbuntu_20.04/ / > /etc/apt/sources.list.d/security:zeek.list
              curl -fsSL https://download.opensuse.org/repositories/security:zeek/xUbuntu_20.04/Release.key | gpg --dearmor > /etc/apt/trusted.gpg.d/security_zeek.gpg
              apt-get $apt_opts update
              apt-get $apt_opts install -y zeek
            fi
            
            if [ -x /opt/zeek/bin/zeek ]; then
              printf "export PATH=/opt/zeek/bin:$PATH\n" > /etc/profile.d/zeek.sh
              chmod +x /etc/profile.d/zeek.sh
              ln -sf /opt/zeek/bin/zeek /usr/local/bin/zeek || true
            fi
            
            zeek_bin="$(command -v zeek || echo /opt/zeek/bin/zeek)"
            echo "[sensor] zeek=$($zeek_bin --version 2>/dev/null | head -1)"
            echo "[sensor] tcpdump=$(tcpdump --version 2>/dev/null | head -1)"
        """
        try:
            self._run_b64("sensor", script, timeout=480, require_root=True)
            logger.info("[SensorAgent] Ferramentas preparadas.")
        except Exception as e:
            logger.error(f"[SensorAgent] ensure_tools falhou: {e}")
            raise

    def arm_capture(self, rotate_sec=300, rotate_mb=100, victim_ip=None, attacker_ip=None):
        """
        Sobe tcpdump + Zeek na interface correta e valida saúde.
        """
        script = f"""
set -Eeuo pipefail
log() {{ printf "[sensor] %s\\n" "$*"; }}

mkdir -p /var/log/pcap /var/log/zeek /var/run/sensor
chmod 0755 /var/log/pcap /var/log/zeek || true

attacker="{attacker_ip or ""}"
victim="{victim_ip or ""}"

iface=""
if [ -n "$victim" ]; then
  iface="$(ip route get "$victim" 2>/dev/null | awk '/dev/ {{for(i=1;i<=NF;i++) if($i=="dev"){{print $(i+1); exit}}}}')"
fi
if [ -z "$iface" ] && [ -n "$attacker" ]; then
  iface="$(ip route get "$attacker" 2>/dev/null | awk '/dev/ {{for(i=1;i<=NF;i++) if($i=="dev"){{print $(i+1); exit}}}}')"
fi
if [ -z "$iface" ]; then
  iface="$(ip -br link | awk '/UP/ && !/LOOPBACK/ {{print $1; exit}}')"
fi
log "iface usada: $iface"
[ -n "$iface" ] || (echo "[sensor] ERRO: iface vazia" >&2; exit 2)

pkill -x tcpdump 2>/dev/null || true
pkill -x zeek    2>/dev/null || true
sleep 0.4

nohup /usr/sbin/tcpdump -i "$iface" -s 0 -U -nn \
  -w /var/log/pcap/exp_%Y%m%d_%H%M%S.pcap \
  -G {rotate_sec} -C {rotate_mb} -W 48 \
  >/var/log/pcap/tcpdump.out 2>&1 & echo $! >/var/run/sensor/tcc_tcpdump.pid

sleep 0.8
pgrep -x tcpdump >/dev/null || (echo "[sensor] ERRO tcpdump" >&2; tail -n 80 /var/log/pcap/tcpdump.out; exit 3)

ZEEXE="$(command -v zeek || echo /opt/zeek/bin/zeek)"
: > /var/log/zeek/zeek.out
nohup "$ZEEXE" -i "$iface" -C -e "redef Log::default_logdir=\\"/var/log/zeek\\"" \
  >>/var/log/zeek/zeek.out 2>&1 & echo $! >/var/run/sensor/tcc_zeek.pid

sleep 1.2
pgrep -fa "zeek -i $iface" >/dev/null || (echo "[sensor] ERRO zeek" >&2; tail -n 120 /var/log/zeek/zeek.out; exit 4)

echo "[health] processos:"
pgrep -fa "tcpdump -i" || true
pgrep -fa "zeek -i"    || true

echo "[health] últimos arquivos:"
ls -lh /var/log/pcap/*.pcap 2>/dev/null | tail -n 5 || true
ls -lh /var/log/zeek/*.log  2>/dev/null | tail -n 10 || true

echo "[health] amostra conn.log:"
[ -f /var/log/zeek/conn.log ] && tail -n 5 /var/log/zeek/conn.log || echo "[warn] conn.log ainda não existe."
"""
        try:
            self._run_b64("sensor", script, timeout=180, require_root=True)
            logger.info("[SensorAgent] Captura armada com sucesso.")
        except Exception as e:
            logger.error(f"[SensorAgent] arm_capture falhou: {e}")
            raise

    def health(self) -> bool:
        script = r"""
set -e
ok=0
pgrep -x tcpdump >/dev/null && echo "[ok] tcpdump vivo" || echo "[err] tcpdump morto"
pgrep -x zeek    >/dev/null && echo "[ok] zeek vivo"    || echo "[err] zeek morto"
test -s /var/log/zeek/conn.log && echo "[ok] conn.log existe" || echo "[warn] conn.log ausente/vazio"
tail -n 3 /var/log/zeek/conn.log 2>/dev/null || true
"""
        try:
            self._run_b64("sensor", script, timeout=20)
            logger.info("[SensorAgent] Health executado.")
            # Mantemos retorno True; se quiser, você pode capturar stdout e inferir.
            return True
        except Exception as e:
            logger.error(f"[SensorAgent] Health falhou: {e}")
            return False