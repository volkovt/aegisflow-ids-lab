import logging
logger = logging.getLogger("[Safety]")

def _lab_iface(ssh, host="attacker") -> str:
    try:
        cmd = r"ip -br a | awk '/192\.168\.56\./{print $1; exit}'"
        out = ssh.run_command(host, cmd, timeout=10).strip()
        return out or "enp0s8"
    except Exception as e:
        logger.warning(f"[Safety] iface lab não detectada: {e}")
        return "enp0s8"

def apply_attacker_egress_guard(ssh, victim_ip: str, sensor_ip: str | None = None):
    """
    Permite só tráfego do atacante -> vítima (e opcional sensor) na rede do lab.
    Bloqueia host do lab (192.168.56.1) e demais RFC1918 na iface do lab.
    """
    iface = _lab_iface(ssh, "attacker")
    try:
        logger.info(f"[Safety] Ativando egress guard na {iface}...")
        allow_sensor = f"sudo iptables -A TCC_LAB_EGRESS -d {sensor_ip} -j ACCEPT;" if sensor_ip else ""
        rules = f"""
        sudo iptables -N TCC_LAB_EGRESS 2>/dev/null || true
        sudo iptables -F TCC_LAB_EGRESS
        sudo iptables -D OUTPUT -o {iface} -j TCC_LAB_EGRESS 2>/dev/null || true
        sudo iptables -A OUTPUT -o {iface} -j TCC_LAB_EGRESS
        sudo iptables -A TCC_LAB_EGRESS -d {victim_ip} -j ACCEPT;
        {allow_sensor}
        sudo iptables -A TCC_LAB_EGRESS -d 192.168.56.1 -j DROP
        sudo iptables -A TCC_LAB_EGRESS -d 10.0.0.0/8 -j DROP
        sudo iptables -A TCC_LAB_EGRESS -d 172.16.0.0/12 -j DROP
        sudo iptables -A TCC_LAB_EGRESS -d 192.168.0.0/16 -j DROP
        sudo iptables -A TCC_LAB_EGRESS -j DROP
        """
        ssh.run_command("attacker", "bash -lc \"" + rules.replace("\n", " ") + "\"", timeout=20)
        logger.info("[Safety] Egress guard ativo.")
    except Exception as e:
        logger.error(f"[Safety] Falha ao ativar egress guard: {e}")
        raise

def remove_attacker_egress_guard(ssh):
    iface = _lab_iface(ssh, "attacker")
    try:
        ssh.run_command(
            "attacker",
            f"sudo iptables -D OUTPUT -o {iface} -j TCC_LAB_EGRESS 2>/dev/null || true; "
            f"sudo iptables -F TCC_LAB_EGRESS 2>/dev/null || true; "
            f"sudo iptables -X TCC_LAB_EGRESS 2>/dev/null || true",
            timeout=10
        )
        logger.info("[Safety] Egress guard removido.")
    except Exception as e:
        logger.warning(f"[Safety] Remoção do egress guard falhou: {e}")

def toggle_attacker_nat(ssh, enable: bool):
    """
    Desativa/ativa a iface NAT do atacante (para impedir saída à Internet durante o ataque).
    """
    try:
        dev = ssh.run_command("attacker", r"ip r | awk '/^default/{print $5; exit}'", timeout=8).strip() or "enp0s3"
        if enable:
            ssh.run_command("attacker", f"sudo ip link set {dev} up; sudo dhclient -r {dev}; sudo dhclient {dev}", timeout=20)
            logger.info(f"[Safety] NAT {dev} reativado.")
        else:
            ssh.run_command("attacker", f"sudo ip link set {dev} down", timeout=8)
            logger.info(f"[Safety] NAT {dev} desativado durante o ataque.")
    except Exception as e:
        logger.warning(f"[Safety] toggle_attacker_nat falhou: {e}")
