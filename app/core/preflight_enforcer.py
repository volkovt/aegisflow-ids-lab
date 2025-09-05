# preflight.py
import logging
import time
from datetime import datetime, timedelta
from typing import Iterable

logger = logging.getLogger("[Preflight]")

class PreflightEnforcer:
    def __init__(self, vagrant_manager, lab_dir: str, max_age_minutes: int = 10):
        self.vagrant = vagrant_manager
        self.lab_dir = lab_dir
        self.max_age = timedelta(minutes=max_age_minutes)
        self._cache = {}  # {vm: datetime da última validação}

    def ensure(self, vm_names: Iterable[str]) -> None:
        """
        Garante que o SSH está pronto em todas as VMs solicitadas.
        Roda sempre; usa um cache curto para não ficar repetindo desnecessariamente.
        """
        now = datetime.now()
        for name in vm_names:
            last_ok = self._cache.get(name)
            if last_ok and (now - last_ok) < self.max_age:
                continue
            try:
                logger.info(f"[Preflight] Checando SSH de {name}...")
                self.vagrant.wait_ssh_ready(name, self.lab_dir, attempts=10, delay_s=3)
                # pequeno respiro para o sshd estabilizar I/O após o primeiro handshake
                time.sleep(0.5)
                self._cache[name] = datetime.now()
                logger.info(f"[Preflight] {name} OK.")
            except Exception as e:
                logger.error(f"[Preflight] Falha em {name}: {e}")
                raise
