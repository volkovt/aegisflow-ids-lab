# data_collector.py (trecho)
import logging
import threading
import time
from typing import Dict

logger = logging.getLogger("[Collector]")

class WarmupCoordinator:
    def __init__(self, warmup_window_s: int = 30):
        self._boot_t0: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._warmup = warmup_window_s
        self._serial_gate = threading.BoundedSemaphore(value=1)  # só 1 durante o warmup

    def mark_boot(self, name: str):
        with self._lock:
            self._boot_t0[name] = time.time()

    def _in_warmup(self, name: str) -> bool:
        with self._lock:
            t0 = self._boot_t0.get(name, 0.0)
        return (time.time() - t0) < self._warmup

    def collect(self, name: str, do_collect_fn):
        """
        Executa a coleta para 'name'. Se estiver no warmup window,
        força execução serial. Depois libera paralelismo normal.
        """
        gate = self._serial_gate if self._in_warmup(name) else _DUMMY_GATE
        with gate:
            try:
                logger.info(f"[Warmup] Coleta em {name} (warmup={self._in_warmup(name)})")
                return do_collect_fn()
            except Exception as e:
                logger.error(f"[Warmup] Erro coleta {name}: {e}")
                raise

class _DummyGate:
    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb): return False

_DUMMY_GATE = _DummyGate()
