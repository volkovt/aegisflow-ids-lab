# lab/orchestrator/actions/attack.py
from __future__ import annotations
from dataclasses import dataclass
import logging
from pathlib import Path

from app.core.logger_setup import setup_logger

logger = setup_logger(Path('.logs'), name="[AttackExecutor]")

@dataclass
class AttackExecutor:
    def __init__(self, ssh_manager):
        self.ssh = ssh_manager

    @staticmethod
    def _normalize(cmd: str) -> str:
        """
        Normaliza quebras de linha para LF e remove espaços à direita.
        Isso evita que CRLF do Windows quebre o shell remoto.
        """
        if cmd is None:
            return ""
        cmd = cmd.replace("\r\n", "\n").replace("\r", "\n")
        lines = [ln.rstrip() for ln in cmd.splitlines()]
        return "\n".join(lines)

    def run_cmd(self, host: str, cmd: str, timeout: int):
        """
        Executa 'cmd' no 'host' (por role/nome do alvo).
        """
        try:
            norm = self._normalize(cmd)
            logger.info(f"[attack] host={host} timeout={timeout}s\n---BEGIN CMD---\n{norm}\n---END CMD---")
            out = self.ssh.run_command(host, norm, timeout=timeout)
            if out:
                for line in (out or "").splitlines():
                    logger.info(line)
        except Exception as e:
            logger.error(f"[attack] erro no comando no host '{host}': {e}")
            raise