# lab/orchestrator/actions/attack.py
from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.logger_setup import setup_logger
from lab.orchestrator.yaml_loader import _join_shell_lines

logger = setup_logger(Path('.logs'), name="[AttackExecutor]")

@dataclass
class AttackExecutor:
    def __init__(self, ssh_manager):
        self.ssh = ssh_manager

    @staticmethod
    def _normalize_shell(cmd):
        try:
            if isinstance(cmd, list):
                return _join_shell_lines(cmd)
            return str(cmd).strip()
        except Exception as e:
            logger.warning(f"[_normalize_shell] {e}")
            return str(cmd)

    def run_cmd(self, host: str, cmd: str, timeout: int):
        try:
            t0 = time.time()
            norm = self._normalize_shell(cmd)
            if norm.strip().startswith(("bash -lc", "timeout ")):
                wrapped = norm
            else:
                wrapped = f"bash -lc {shlex.quote(norm)}"
            logger.info(f"[attack] host={host} timeout={timeout}s\n---BEGIN CMD---\n{wrapped}\n---END CMD---")
            out = self.ssh.run_command(host, wrapped, timeout=timeout)
            if out:
                for line in (out or "").splitlines():
                    logger.info(line)
            logger.info(f"[attack] completed host={host} in {time.time()-t0:.2f}s")
        except Exception as e:
            logger.error(f"[attack] erro no comando no host '{host}': {e}")
            raise