# File: `scripts/sensor_init.sh`
#!/usr/bin/env bash
set -euo pipefail

echo "[sensor] killing zeek/tcpdump if exist..."
pkill -x zeek 2>/dev/null || true
pkill -x tcpdump 2>/dev/null || true
sleep 0.5

echo "[sensor] removing old zeek logs..."
rm -f /var/log/zeek/*.log 2>/dev/null || true
: > /var/log/zeek/zeek.out 2>/dev/null || true

mkdir -p /var/log/pcap /var/run/sensor
chmod 0755 /var/log/pcap /var/log/zeek /var/run/sensor || true

victim=192.168.56.12; attacker=192.168.56.11

iface=$(ip route get "$victim" 2>/dev/null | awk '/dev/ {for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')
[ -z "$iface" ] && iface=$(ip -br link | awk '/UP/ && !/LOOPBACK/ {print $1; exit}')

echo "[sensor] selected iface: $iface"

nohup /usr/sbin/tcpdump -i "$iface" -s 0 -U -nn -w /var/log/pcap/exp_%Y%m%d_%H%M%S.pcap -G 300 -C 100 -W 48 >/var/log/pcap/tcpdump.out 2>&1 & echo $! >/var/run/sensor/tcc_tcpdump.pid
sleep 0.8
pgrep -x tcpdump >/dev/null || { echo "[sensor] ERRO tcpdump"; tail -n 80 /var/log/pcap/tcpdump.out; exit 3; }

echo "=== EXP $(date -Is) START (victim=$victim attacker=$attacker iface=$iface) ===" >> /var/log/zeek/zeek.out

ZEEXE=$(command -v zeek || echo /opt/zeek/bin/zeek)
nohup "$ZEEXE" -i "$iface" -C -e "redef Log::default_logdir=\"/var/log/zeek\"" >>/var/log/zeek/zeek.out 2>&1 & echo $! >/var/run/sensor/tcc_zeek.pid
sleep 1.2
pgrep -fa "zeek -i" >/dev/null || { echo "[sensor] ERRO zeek"; tail -n 120 /var/log/zeek/zeek.out; exit 4; }

echo "[sensor] processos ativos:"
pgrep -fa "tcpdump -i" || true
pgrep -fa "zeek -i" || true

echo "[sensor] arquivos zeek:"
ls -lh /var/log/zeek 2>/dev/null || true

echo "[sensor] amostra conn.log:"
[ -f /var/log/zeek/conn.log ] && tail -n 8 /var/log/zeek/conn.log || echo "(conn.log não existe ainda)"