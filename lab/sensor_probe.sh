# File: scripts/sensor_probe.sh
#!/usr/bin/env bash
set -euo pipefail

victim=192.168.56.12

iface=$(ip route get "$victim" 2>/dev/null | awk '/dev/ {for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')
[ -z "$iface" ] && iface=$(ip -br link | awk '/UP/ && !/LOOPBACK/ {print $1; exit}')

echo "[sensor] probe iface: $iface"
timeout 20 tcpdump -vv -ni "$iface" "tcp and host $victim and port 22" -nn -c 200 || true
