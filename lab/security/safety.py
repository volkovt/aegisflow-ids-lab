import logging
logger = logging.getLogger("[Safety]")

CHAIN = "TCC_EGRESS"
LAB_CIDR = "192.168.56.0/24"

def _run(ssh, cmd: str, **kw):
    return ssh.run_command("attacker", cmd, **kw)

def apply_attacker_egress_guard(ssh, victim_ip: str = None, sensor_ip: str = None, lab_cidr: str = LAB_CIDR):
    try:
        logger.info("[Safety] Aplicando egress guard (cadeia %s)...", CHAIN)

        # 1) Garante a chain e o jump uma única vez
        _run(ssh, f"sudo iptables -w -N {CHAIN} 2>/dev/null || true", timeout=10)
        _run(ssh, f"sudo iptables -w -F {CHAIN}", timeout=10)
        _run(ssh, f"sudo iptables -C OUTPUT -j {CHAIN} 2>/dev/null || sudo iptables -I OUTPUT 1 -j {CHAIN}", timeout=10)

        # 2) Regras de retorno seguro
        base_rules = [
            f"sudo iptables -w -A {CHAIN} -o lo -j RETURN",
            f"sudo iptables -w -A {CHAIN} -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN",
            f"sudo iptables -w -A {CHAIN} -d {lab_cidr} -j RETURN",   # libera toda a rede do lab
            "sudo iptables -w -A {chain} -p udp --dport 53 -j RETURN".format(chain=CHAIN),  # DNS (se precisar)
        ]

        for r in base_rules:
            _run(ssh, r, timeout=10)

        # 3) Whitelist explícita (opcional, redundante se lab_cidr já cobre)
        if victim_ip:
            _run(ssh, f"sudo iptables -w -A {CHAIN} -d {victim_ip} -j RETURN", timeout=10)
        if sensor_ip:
            _run(ssh, f"sudo iptables -w -A {CHAIN} -d {sensor_ip} -j RETURN", timeout=10)

        # 4) Política de bloqueio padrão
        _run(ssh, f"sudo iptables -w -A {CHAIN} -j REJECT --reject-with icmp-port-unreachable", timeout=10)

        logger.info("[Safety] Egress guard ativo (liberado %s / alvo(s) do lab).", lab_cidr)
    except Exception as e:
        logger.error(f"[Safety] Falha ao aplicar egress guard: {e}")
        raise

def remove_attacker_egress_guard(ssh):
    try:
        logger.info("[Safety] Removendo egress guard...")
        _run(ssh, f"sudo iptables -D OUTPUT -j {CHAIN} 2>/dev/null || true", timeout=10)
        _run(ssh, f"sudo iptables -F {CHAIN} 2>/dev/null || true", timeout=10)
        _run(ssh, f"sudo iptables -X {CHAIN} 2>/dev/null || true", timeout=10)
        logger.info("[Safety] Egress guard removido.")
    except Exception as e:
        logger.warning(f"[Safety] Falha ao remover egress guard: {e}")

def toggle_attacker_nat(ssh, enable: bool, victim_ip: str = None, sensor_ip: str = None):
    """
    Compatibilidade com o Runner:
      - enable=False -> isola (aplica guard com exceções do lab)
      - enable=True  -> restaura (remove guard)
    """
    if enable:
        remove_attacker_egress_guard(ssh)
    else:
        apply_attacker_egress_guard(ssh, victim_ip=victim_ip, sensor_ip=sensor_ip)
