import threading
import logging
from PySide6 import QtCore
from PySide6.QtCore import Signal
from concurrent.futures import CancelledError

logger = logging.getLogger("VagrantLabUI")

class DatasetController(QtCore.QObject):
    started = Signal()
    finished = Signal(str)  # "ok", "error", "aborted"

    def __init__(self, runner):
        super().__init__()
        self.runner = runner
        self._worker = None
        self.cancel_event = threading.Event()

    def start(self, experiment_yaml: str, out_dir: str):
        if self._worker and self._worker.is_alive():
            logger.warning("[Dataset] Já em execução.")
            return
        self.cancel_event.clear()
        self._worker = threading.Thread(
            target=self._run_safe, args=(experiment_yaml, out_dir), daemon=True
        )
        self._worker.start()
        self.started.emit()

    def cancel(self):
        if self._worker and self._worker.is_alive():
            logger.warning("[Dataset] Cancelamento solicitado pelo usuário.")
            self.cancel_event.set()
            try:
                self.runner.ssh.cancel_all_running()
            except Exception as e:
                logger.error(f"[Dataset] Falha ao cancelar SSHs ativos: {e}")

    def _run_safe(self, experiment_yaml: str, out_dir: str):
        status = "ok"
        try:
            self.runner.run_from_yaml(
                experiment_yaml, out_dir, cancel_event=self.cancel_event
            )
        except CancelledError:
            status = "aborted"
            logger.warning("[Dataset] Execução abortada pelo usuário.")
        except Exception as e:
            status = "error"
            logger.error(f"[Dataset] Erro na execução: {e}")
        finally:
            self.finished.emit(status)
