# File: scripts/sensor_collect.sh
#!/usr/bin/env bash
set -euo pipefail

echo "=== CONN last 60 lines ==="
tail -n 60 /var/log/zeek/conn.log 2>/dev/null || true
echo
echo "=== SSH last 60 lines ==="
tail -n 60 /var/log/zeek/ssh.log 2>/dev/null || true
echo
echo "=== Zeek out last 60 lines ==="
tail -n 60 /var/log/zeek/zeek.out 2>/dev/null || true
echo
echo "=== PCAP list/sizes ==="
ls -lh /var/log/pcap 2>/dev/null || true