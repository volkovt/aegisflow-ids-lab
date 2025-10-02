# lab/orchestrator/actions/utils.py
from __future__ import annotations

import re
from typing import Dict, Optional
import logging

logger = logging.getLogger("[Runner]")
if not hasattr(logger, "warn"):
    logger.warn = logger.warning

def substitute_placeholders(cmd: str, ips: Dict[str, str]) -> str:
    try:
        return (cmd or "")\
            .replace("{attacker_ip}", ips.get("attacker", ""))\
            .replace("{sensor_ip}",   ips.get("sensor", ""))\
            .replace("{victim_ip}",   ips.get("victim", ""))
    except Exception as e:
        logger.warning(f"[Runner] Falha no substitute_placeholders: {e}")
        return cmd or ""

def cancelled(cancel_event) -> bool:
    try:
        return bool(cancel_event and cancel_event.is_set())
    except Exception:
        return False


def format_only_keys(template: str, mapping: dict[str, str], keys: set[str]) -> str:
    # só troca {chave} quando a chave está em `keys`
    pattern = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
    def repl(m):
        k = m.group(1)
        return str(mapping[k]) if k in keys else m.group(0)
    return pattern.sub(repl, template)