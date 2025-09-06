#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "[sensor] >>> Preparando ambiente de captura..."

# 1) Atualiza índices e instala pacotes básicos
apt-get update -y
apt-get install -y --no-install-recommends tcpdump curl jq coreutils gawk iproute2 ethtool gnupg lsb-release

# 2) Tenta instalar Zeek (primeiro tenta do repositório nativo)
if ! apt-get install -y --no-install-recommends zeek; then
  . /etc/os-release || true

  # Debian 12 (bookworm)
  if [ "${ID:-}" = "debian" ] && [ "${VERSION_CODENAME:-}" = "bookworm" ]; then
    curl -fsSL https://download.opensuse.org/repositories/security:/zeek/Debian_12/Release.key \
      | gpg --dearmor > /usr/share/keyrings/zeek.gpg
    printf "deb [signed-by=/usr/share/keyrings/zeek.gpg] https://download.opensuse.org/repositories/security:/zeek/Debian_12/ / \n" \
      > /etc/apt/sources.list.d/zeek.list
  fi

  # Debian 11 (bullseye)  <<< NOVO
  if [ "${ID:-}" = "debian" ] && [ "${VERSION_CODENAME:-}" = "bullseye" ]; then
    curl -fsSL https://download.opensuse.org/repositories/security:/zeek/Debian_11/Release.key \
      | gpg --dearmor > /usr/share/keyrings/zeek.gpg
    printf "deb [signed-by=/usr/share/keyrings/zeek.gpg] https://download.opensuse.org/repositories/security:/zeek/Debian_11/ / \n" \
      > /etc/apt/sources.list.d/zeek.list
  fi

  # Ubuntu 22.04 (jammy)
  if [ "${ID:-}" = "ubuntu" ] && [ "${VERSION_CODENAME:-}" = "jammy" ]; then
    curl -fsSL https://download.opensuse.org/repositories/security:/zeek/xUbuntu_22.04/Release.key \
      | gpg --dearmor > /usr/share/keyrings/zeek.gpg
    printf "deb [signed-by=/usr/share/keyrings/zeek.gpg] https://download.opensuse.org/repositories/security:/zeek/xUbuntu_22.04/ / \n" \
      > /etc/apt/sources.list.d/zeek.list
  fi

  # Ubuntu 20.04 (focal)
  if [ "${ID:-}" = "ubuntu" ] && [ "${VERSION_CODENAME:-}" = "focal" ]; then
    curl -fsSL https://download.opensuse.org/repositories/security:/zeek/xUbuntu_20.04/Release.key \
      | gpg --dearmor > /usr/share/keyrings/zeek.gpg
    printf "deb [signed-by=/usr/share/keyrings/zeek.gpg] https://download.opensuse.org/repositories/security:/zeek/xUbuntu_20.04/ / \n" \
      > /etc/apt/sources.list.d/zeek.list
  fi

  # Atualiza índices e tenta instalar novamente
  apt-get update -y || true
  apt-get install -y --no-install-recommends zeek || true
fi

# 3) Garante pastas de logs usadas pelo pipeline
mkdir -p /var/log/pcap /var/log/zeek

# 4) Opcional: permitir tcpdump sem root
if command -v setcap >/dev/null 2>&1; then
  setcap cap_net_raw,cap_net_admin=eip /usr/sbin/tcpdump || true
fi

# 5) Mostra versões instaladas
echo "[sensor] >>> Ferramentas instaladas:"
tcpdump --version | head -1 || true
zeek --version || true

# --- Resolução de PATH do Zeek (quando instalado em /opt/zeek) ---
if [ -x /opt/zeek/bin/zeek ]; then
  echo "[sensor] >>> Ajustando PATH para /opt/zeek/bin (Zeek OBS)..."
  echo 'export PATH=/opt/zeek/bin:$PATH' > /etc/profile.d/zeek.sh
  chmod +x /etc/profile.d/zeek.sh

  # symlink universal
  if [ ! -e /usr/local/bin/zeek ]; then
    ln -s /opt/zeek/bin/zeek /usr/local/bin/zeek
  fi

  # recarrega tabela de comandos do shell atual
  hash -r || true
fi

# 5) Sanidade (mostra versões instaladas)
echo "[sensor] >>> Ferramentas instaladas:"
tcpdump --version | head -1 || true
command -v zeek >/dev/null 2>&1 && zeek --version || { echo "zeek: NÃO encontrado (usar /opt/zeek/bin/zeek se necessário)"; /opt/zeek/bin/zeek --version || true; }
command -v jq   >/dev/null 2>&1 && jq --version   || true