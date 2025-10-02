# lab/orchestrator/actions/sensor.py
from __future__ import annotations
from dataclasses import dataclass
import logging
from pathlib import Path

from app.core.logger_setup import setup_logger
from lab.utils import format_only_keys

logger = setup_logger(Path('.logs'), name="[Sensor]")

# sensor.py (trechos relevantes)

SENSOR_INIT_SCRIPT = r"""
    set -euo pipefail
    umask 022
    
    log() { echo "[sensor] $*"; }
    
    # Descobre usuário real (sem sudo)
    USR="$(id -un)"
    BASE1="${HOME}/tcc"
    BASE2="/tmp/tcc"
    
    ensure_base() {
      local base="$1"
      local pcap="${base}/pcap" zeek="${base}/zeek" run="${base}/run"
      mkdir -p "$pcap" "$zeek" "$run" 2>/dev/null || true
      chmod 0777 "$pcap" "$zeek" "$run" 2>/dev/null || true
    
      # teste de escrita como usuário
      if ! touch "${pcap}/.__wtest_u" 2>/dev/null; then
        return 1
      fi
      rm -f "${pcap}/.__wtest_u" 2>/dev/null || true
    
      # se tivermos sudo, testa também escrita por root (para pegar root-squash)
      if sudo -n true 2>/dev/null; then
        if ! sudo -n /bin/sh -c "echo test > '${pcap}/.__wtest_r'" 2>/dev/null; then
          return 1
        fi
        sudo -n rm -f "${pcap}/.__wtest_r" 2>/dev/null || true
      fi
    
      echo "$base"
      return 0
    }
    
    # Escolhe BASE preferindo HOME, caindo para /tmp em caso de squash/permissões
    BASE=""
    if b="$(ensure_base "$BASE1")"; then BASE="$b"; else
      b="$(ensure_base "$BASE2")" || true
      BASE="${b:-$BASE2}"
      mkdir -p "${BASE}/pcap" "${BASE}/zeek" "${BASE}/run" 2>/dev/null || true
      chmod 0777 "${BASE}/pcap" "${BASE}/zeek" "${BASE}/run" 2>/dev/null || true
    fi
    
    LOGPCAP="${BASE}/pcap"
    LOGZEEK="${BASE}/zeek"
    RUNDIR="${BASE}/run"
    
    # SUDO se disponível; caso contrário vazio
    if sudo -n true 2>/dev/null; then
      SUDO="sudo -n"
    else
      SUDO=""
    fi
    
    log "base escolhida: ${BASE}"
    log "killing zeek/tcpdump if exist..."
    $SUDO pkill -x zeek 2>/dev/null || true
    $SUDO pkill -x tcpdump 2>/dev/null || true
    sleep 0.4
    
    log "limpando logs antigos de zeek..."
    rm -f "${LOGZEEK}"/*.log 2>/dev/null || true
    : > "${LOGZEEK}/zeek.out" 2>/dev/null || true
    
    victim="{victim_ip}"; attacker="{attacker_ip}"
    
    # Descobre melhor interface pelo caminho até a vítima
    iface=$(ip route get "$victim" 2>/dev/null | awk '/dev/ {for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')
    [ -z "$iface" ] && iface=$(ip -br link | awk '/UP/ && !/LOOPBACK/ {print $1; exit}')
    log "selected iface: $iface"
    
    # Segurança defensiva: garante que diretórios sejam graváveis mesmo para root-squash
    chmod 0777 "${LOGPCAP}" "${LOGZEEK}" "${RUNDIR}" 2>/dev/null || true
    
    # ---- TCPDUMP ----
    # -Z $USR: após abrir o socket como root (sudo), derruba privilégios para o usuário real
    # Arquivo rotativo com strftime + janela (-G) + número de arquivos (-W) + limite de tamanho (-C)
    nohup $SUDO /usr/sbin/tcpdump -i "$iface" -s 0 -U -nn \
      -w "${LOGPCAP}/exp_%Y%m%d_%H%M%S.pcap" \
      -G 300 -C 100 -W 48 -Z "$USR" \
      > "${LOGPCAP}/tcpdump.out" 2>&1 \
      & echo $! > "${RUNDIR}/tcc_tcpdump.pid"
    
    sleep 0.8
    if ! pgrep -x tcpdump >/dev/null; then
      log "ERRO tcpdump (veja ${LOGPCAP}/tcpdump.out abaixo):"
      tail -n 200 "${LOGPCAP}/tcpdump.out" || true
      log "dica: se este host usa root-squash no HOME, manteremos a captura em ${BASE} (pode ser /tmp)."
      log "dica2: considere aplicar 'setcap cap_net_raw,cap_net_admin+eip /usr/sbin/tcpdump' e rodar sem sudo."
      exit 3
    fi
    
    # ---- ZEEK ----
    ZEEXE=$(command -v zeek || echo /opt/zeek/bin/zeek)
    nohup $SUDO "$ZEEXE" -i "$iface" -C \
      -e "redef Log::default_logdir=\"${LOGZEEK}\"" \
      >> "${LOGZEEK}/zeek.out" 2>&1 \
      & echo $! > "${RUNDIR}/tcc_zeek.pid"
    
    sleep 1.0
    pgrep -fa "zeek -i" >/dev/null || {
      log "ERRO zeek"; tail -n 200 "${LOGZEEK}/zeek.out" || true; exit 4;
    }
    
    log "captura ativa (tcpdump+zeek)."
    log "[paths] pcap=${LOGPCAP} zeek=${LOGZEEK} run=${RUNDIR}"
"""


SENSOR_STOP_SCRIPT = r"""
    set -euo pipefail
    if sudo -n true 2>/dev/null; then
      SUDO="sudo -n"
    else
      SUDO=""
    fi
    $SUDO pkill -x tcpdump 2>/dev/null || true
    $SUDO pkill -x zeek 2>/dev/null || true
    sleep 0.5
    echo "[sensor] stopped."
"""

SENSOR_COLLECT_SCRIPT = r"""
    set -euo pipefail
    BASE="${HOME}/tcc"
    LOGPCAP="${BASE}/pcap"
    LOGZEEK="${BASE}/zeek"
    
    echo "=== CONN last 60 lines ==="
    tail -n 60 "${LOGZEEK}/conn.log" 2>/dev/null || true
    echo
    echo "=== SSH last 60 lines ==="
    tail -n 60 "${LOGZEEK}/ssh.log" 2>/dev/null || true
    echo
    echo "=== Zeek out last 120 lines ==="
    tail -n 120 "${LOGZEEK}/zeek.out" 2>/dev/null || true
    echo
    echo "=== PCAP list/sizes ==="
    ls -lh "${LOGPCAP}" 2>/dev/null || true
"""


@dataclass
class SensorAgent:

    def __init__(self, ssh_manager, name: str = "sensor"):
        self.ssh = ssh_manager
        self.name = name

    def sanitize_and_start(self, victim_ip: str, attacker_ip: str, timeout: int = 60):
        import traceback
        try:
            logger.info("[sensor] inicializando captura…")
            # Evita .format() para não quebrar as chaves do awk/bash
            script = format_only_keys(
                SENSOR_INIT_SCRIPT,
                {"victim_ip": victim_ip, "attacker_ip": attacker_ip},
                {"victim_ip", "attacker_ip"}
            )
            out = self.ssh.run_command(self.name, script, timeout=timeout)
            for line in (out or "").splitlines():
                logger.info(line)
        except Exception as e:
            logger.error(f"[sensor] Falha ao iniciar captura: {e}")
            logger.error(traceback.format_exc())
            raise

    def stop(self):
        try:
            self.ssh.run_command(self.name, SENSOR_STOP_SCRIPT, timeout=30)
            logger.info("[sensor] captura parada.")
        except Exception as e:
            logger.warning(f"[sensor] stop: {e}")

    def collect_snapshot(self):
        try:
            out = self.ssh.run_command(self.name, SENSOR_COLLECT_SCRIPT, timeout=40)
            logger.info(out or "")
        except Exception as e:
            logger.warning(f"[sensor] collect: {e}")
