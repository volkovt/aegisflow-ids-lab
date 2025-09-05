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

def _run(ssh, cmd: str, **kw):
    """
    Executa comando no 'attacker' tentando usar a API cancelável quando existir.
    Aceita 'timeout' ou 'timeout_s'.
    """
    try:
        to_s = kw.get("timeout_s", None)
        if to_s is None:
            to_s = kw.get("timeout", 20)

        if hasattr(ssh, "run_command_cancellable"):
            return ssh.run_command_cancellable("attacker", cmd, timeout_s=int(to_s))
        return ssh.run_command("attacker", cmd, timeout=int(to_s))
    except Exception as e:
        logger.error(f"[Safety] _run falhou: {e}")
        raise

def apply_attacker_egress_guard(ssh, victim_ip: str | None = None, sensor_ip: str | None = None):
    try:
        logger.info("[Safety] Aplicando egress guard (cadeia TCC_EGRESS)...")
        cmd = """
            set -e
            sudo iptables -N TCC_EGRESS 2>/dev/null || true
            sudo iptables -F TCC_EGRESS
            sudo iptables -A TCC_EGRESS -o lo -j RETURN
            sudo iptables -A TCC_EGRESS -p tcp --dport 22 -j RETURN
            [ -n "{V}" ] && sudo iptables -A TCC_EGRESS -d "{V}" -j RETURN || true
            [ -n "{S}" ] && sudo iptables -A TCC_EGRESS -d "{S}" -j RETURN || true
            sudo iptables -A TCC_EGRESS -j DROP
            sudo iptables -C OUTPUT -j TCC_EGRESS 2>/dev/null || sudo iptables -I OUTPUT 1 -j TCC_EGRESS
            """.format(V=victim_ip or "", S=sensor_ip or "")
        _run(ssh, cmd)
        logger.info("[Safety] Egress guard ativo.")
    except Exception as e:
        logger.error(f"[Safety] Falha ao aplicar egress guard: {e}")
        raise

def remove_attacker_egress_guard(ssh):
    try:
        logger.info("[Safety] Removendo egress guard (TCC_EGRESS)...")
        cmd = """bash -lc '
            sudo iptables -D OUTPUT -j TCC_EGRESS 2>/dev/null || true
            sudo iptables -F TCC_EGRESS 2>/dev/null || true
            sudo iptables -X TCC_EGRESS 2>/dev/null || true
            '"""
        _run(ssh, cmd)
        logger.info("[Safety] Egress guard removido.")
    except Exception as e:
        logger.warning(f"[Safety] Falha ao remover egress guard: {e}")

def toggle_attacker_nat(ssh, enable: bool):
    """
    Mantido por compatibilidade com o Runner. Aqui só chamamos o guard:
    - enable=False -> ativa guard (isola)
    - enable=True  -> remove guard (restaura)
    """
    if enable:
        remove_attacker_egress_guard(ssh)
    else:
        apply_attacker_egress_guard(ssh, victim_ip=None, sensor_ip=None)