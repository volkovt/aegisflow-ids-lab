# lab/orchestrator/actions/sensor.py
from __future__ import annotations
from dataclasses import dataclass
import logging
from pathlib import Path

from app.core.logger_setup import setup_logger
from lab.utils import format_only_keys

logger = setup_logger(Path('.logs'), name="[Sensor]")

SENSOR_INIT_SCRIPT = r"""
    set -euo pipefail
    umask 022

    log() { echo "[sensor] $*"; }
    USR="$(id -un)"
    BASE1="${HOME}/tcc"
    BASE2="/tmp/tcc"

    ensure_base() {
      local base="$1"
      local pcap="${base}/pcap" zeek="${base}/zeek" run="${base}/run"
      mkdir -p "$pcap" "$zeek" "$run" 2>/dev/null || true
      chmod 0777 "$pcap" "$zeek" "$run" 2>/dev/null || true
      if ! touch "${pcap}/.__wtest_u" 2>/dev/null; then return 1; fi
      rm -f "${pcap}/.__wtest_u" 2>/dev/null || true
      if sudo -n true 2>/dev/null; then
        if ! sudo -n /bin/sh -c "echo test > '${pcap}/.__wtest_r'" 2>/dev/null; then return 1; fi
        sudo -n rm -f "${pcap}/.__wtest_r" 2>/dev/null || true
      fi
      echo "$base"
      return 0
    }

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

    if sudo -n true 2>/dev/null; then SUDO="sudo -n"; else SUDO=""; fi

    # ----------------------------
    # Zeek: verificação + instalação com repositórios oficiais se necessário
    # ----------------------------
    ensure_zeek() {
      if command -v zeek >/dev/null 2>&1; then
        log "zeek encontrado no PATH."
        return 0
      fi
      if [ -x "/opt/zeek/bin/zeek" ]; then
        log "zeek encontrado em /opt/zeek/bin/zeek."
        return 0
      fi

      log "zeek não encontrado — preparando instalação…"

      if [ -r /etc/os-release ]; then
        . /etc/os-release
      else
        log "ERRO: /etc/os-release ausente — instalação automática indisponível."
        return 1
      fi

      # Funções auxiliares
      _require_sudo() {
        if [ -z "$SUDO" ]; then
          log "ERRO: sem sudo não-interativo — não é possível instalar pacotes."
          return 1
        fi
      }

      _apt_update_quiet() { $SUDO apt-get update -y || true; }
      _apt_install() { $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" || return 1; }
      _dnf_install() { $SUDO dnf install -y "$@" || $SUDO yum install -y "$@" || return 1; }
      _zypper_install() { $SUDO zypper --non-interactive install "$@" || return 1; }

      _ubuntu_enable_universe() {
        if command -v add-apt-repository >/dev/null 2>&1; then
          $SUDO add-apt-repository -y universe || true
        else
          _apt_install software-properties-common || true
          $SUDO add-apt-repository -y universe || true
        fi
      }

      _add_obs_repo_debian_ubuntu() {
        # Mapas de versão (tentamos VERSION_ID e fallback em VERSION_CODENAME)
        local repo_url="" key_url="" list_path=""
        case "${ID}-${VERSION_ID:-}" in
          ubuntu-24.04) repo_url="https://download.opensuse.org/repositories/security:/zeek/xUbuntu_24.04/";;
          ubuntu-22.04) repo_url="https://download.opensuse.org/repositories/security:/zeek/xUbuntu_22.04/";;
          ubuntu-20.04) repo_url="https://download.opensuse.org/repositories/security:/zeek/xUbuntu_20.04/";;
          debian-12)    repo_url="https://download.opensuse.org/repositories/security:/zeek/Debian_12/";;
          debian-11)    repo_url="https://download.opensuse.org/repositories/security:/zeek/Debian_11/";;
          *) # tentar por codename se id+version não baterem
             case "${ID}-${VERSION_CODENAME:-}" in
               ubuntu-noble) repo_url="https://download.opensuse.org/repositories/security:/zeek/xUbuntu_24.04/";;
               ubuntu-jammy) repo_url="https://download.opensuse.org/repositories/security:/zeek/xUbuntu_22.04/";;
               ubuntu-focal) repo_url="https://download.opensuse.org/repositories/security:/zeek/xUbuntu_20.04/";;
               debian-bookworm) repo_url="https://download.opensuse.org/repositories/security:/zeek/Debian_12/";;
               debian-bullseye) repo_url="https://download.opensuse.org/repositories/security:/zeek/Debian_11/";;
             esac
          ;;
        esac

        if [ -z "$repo_url" ]; then
          log "Aviso: mapeamento OBS não encontrado para ${ID} ${VERSION_ID:-}. Tentando instalação nativa primeiro."
          return 2
        fi

        key_url="${repo_url}Release.key"
        list_path="/etc/apt/sources.list.d/security_zeek.list"

        _require_sudo || return 1
        $SUDO mkdir -p /etc/apt/keyrings || true
        curl -fsSL "$key_url" | gpg --dearmor | $SUDO tee /etc/apt/keyrings/security_zeek.gpg >/dev/null
        echo "deb [signed-by=/etc/apt/keyrings/security_zeek.gpg] ${repo_url} /" | $SUDO tee "$list_path" >/dev/null
        _apt_update_quiet
        return 0
      }

      _add_obs_repo_el() {
        # Enterprise Linux 8/9 (RHEL/CentOS/Rocky/Alma)
        local base=""
        case "${VERSION_ID:-}" in
          9*) base="RHEL_9";;
          8*) base="RHEL_8";;
          *)  base="";;
        esac
        if [ -z "$base" ]; then
          log "Aviso: versão EL não mapeada (${VERSION_ID:-})."
          return 2
        fi
        _require_sudo || return 1
        $SUDO curl -fsSL "https://download.opensuse.org/repositories/security:zeek/${base}/security:zeek.repo" -o /etc/yum.repos.d/zeek.repo
        return 0
      }

      # Tentativas por família de SO
      if command -v apt-get >/dev/null 2>&1 || echo "${ID_LIKE:-}${ID:-}" | grep -qi "debian\|ubuntu"; then
        log "Detectado ambiente Debian/Ubuntu."
        _require_sudo || return 1
        _apt_update_quiet
        # 1) tentar nativo
        if _apt_install zeek; then
          :
        else
          log "zeek não está no repo nativo — habilitando 'universe' (Ubuntu) e adicionando OBS security:zeek…"
          _ubuntu_enable_universe || true
          _add_obs_repo_debian_ubuntu || true
          _apt_update_quiet
          _apt_install zeek || { log "ERRO: falha ao instalar zeek via OBS (Debian/Ubuntu)."; return 1; }
        fi

      elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1 || echo "${ID_LIKE:-}${ID:-}" | grep -qi "rhel\|fedora\|centos"; then
        log "Detectado ambiente RHEL/CentOS/Fedora."
        _require_sudo || return 1
        # Fedora geralmente tem pacote nativo
        if echo "${ID:-}" | grep -qi "fedora"; then
          _dnf_install zeek || {
            log "Pacote não encontrado no Fedora — adicionando OBS…"
            ver="${VERSION_ID:-}"
            if [ -n "$ver" ]; then
              $SUDO dnf config-manager --add-repo "https://download.opensuse.org/repositories/security:zeek/Fedora_${ver}/security:zeek.repo" || true
              _dnf_install zeek || { log "ERRO: falha ao instalar zeek (Fedora via OBS)."; return 1; }
            else
              log "ERRO: Fedora sem VERSION_ID; não foi possível mapear OBS."
              return 1
            fi
          }
        else
          # RHEL/CentOS/Rocky/Alma — habilitar EPEL ajuda dependências
          $SUDO dnf install -y epel-release || $SUDO yum install -y epel-release || true
          if ! _dnf_install zeek; then
            log "Pacote nativo indisponível — adicionando OBS para EL…"
            _add_obs_repo_el || true
            _dnf_install zeek || { log "ERRO: falha ao instalar zeek (EL via OBS)."; return 1; }
          fi
        fi

      elif command -v zypper >/dev/null 2>&1 || echo "${ID_LIKE:-}${ID:-}" | grep -qi "suse"; then
        log "Detectado ambiente openSUSE/SLES."
        _require_sudo || return 1
        $SUDO zypper --non-interactive refresh || true
        _zypper_install zeek || {
          log "Tentando adicionar repositório security:zeek…"
          $SUDO zypper --non-interactive addrepo --refresh "https://download.opensuse.org/repositories/security:/zeek/$(. /etc/os-release; echo ${ID^}_${VERSION_ID})/" zeek || true
          $SUDO zypper --non-interactive refresh || true
          _zypper_install zeek || { log "ERRO: falha ao instalar zeek (openSUSE via OBS)."; return 1; }
        }

      else
        log "Distro não reconhecida para instalação automática. Instale Zeek manualmente."
        return 1
      fi

      if command -v zeek >/dev/null 2>&1 || [ -x "/opt/zeek/bin/zeek" ]; then
        log "zeek instalado com sucesso."
        return 0
      fi

      log "ERRO: zeek ainda indisponível após tentativa de instalação."
      return 1
    }

    log "base escolhida: ${BASE}"
    log "Encerrando restos de zeek/tcpdump (se houver)…"
    $SUDO pkill -x zeek 2>/dev/null || true
    $SUDO pkill -x tcpdump 2>/dev/null || true
    sleep 0.4

    log "Limpando logs antigos de Zeek…"
    rm -f "${LOGZEEK}"/*.log 2>/dev/null || true
    : > "${LOGZEEK}/zeek.out" 2>/dev/null || true

    victim="{victim_ip}"; attacker="{attacker_ip}"
    iface=$(ip route get "$victim" 2>/dev/null | awk '/dev/ {for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')
    [ -z "$iface" ] && iface=$(ip -br link | awk '/UP/ && !/LOOPBACK/ {print $1; exit}')
    log "Interface selecionada: $iface"

    chmod 0777 "${LOGPCAP}" "${LOGZEEK}" "${RUNDIR}" 2>/dev/null || true

    nohup $SUDO /usr/sbin/tcpdump -i "$iface" -s 0 -U -nn \
      -w "${LOGPCAP}/exp_%Y%m%d_%H%M%S.pcap" \
      -G 300 -C 100 -W 48 -Z "$USR" \
      > "${LOGPCAP}/tcpdump.out" 2>&1 \
      & echo $! > "${RUNDIR}/tcc_tcpdump.pid"

    sleep 0.8
    if ! pgrep -x tcpdump >/dev/null; then
      log "ERRO tcpdump (veja ${LOGPCAP}/tcpdump.out):"
      tail -n 200 "${LOGPCAP}/tcpdump.out" || true
      exit 3
    fi

    if ! ensure_zeek; then
      log "Falha ao garantir Zeek — abortando sensor."
      exit 4
    fi

    ZEEXE=$(command -v zeek || echo /opt/zeek/bin/zeek)
    nohup $SUDO "$ZEEXE" -i "$iface" -C \
      -e "redef Log::default_logdir=\"${LOGZEEK}\"" \
      >> "${LOGZEEK}/zeek.out" 2>&1 \
      & echo $! > "${RUNDIR}/tcc_zeek.pid"

    sleep 1.0
    pgrep -fa "zeek -i" >/dev/null || { log "ERRO ao subir Zeek"; tail -n 200 "${LOGZEEK}/zeek.out" || true; exit 4; }

    log "Captura ativa (tcpdump + Zeek)."
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
