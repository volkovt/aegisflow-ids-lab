import threading
import logging
from PySide6 import QtCore
from PySide6.QtCore import Signal
from concurrent.futures import CancelledError

logger = logging.getLogger("VagrantLabUI")

class DatasetController(QtCore.QObject):
    started = Signal()
    finished = Signal(str)  # "ok", "error", "aborted"
    progress = Signal(str)

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
        try:
            self.progress.emit(f"[Dataset] YAML: {experiment_yaml}")
            self.progress.emit(f"[Dataset] Saída: {out_dir}")
        except Exception:
            pass

    def cancel(self):
        if self._worker and self._worker.is_alive():
            logger.warning("[Dataset] Cancelamento solicitado pelo usuário.")
            try:
                self.progress.emit("[Dataset] Cancelamento solicitado pelo usuário.")
            except Exception:
                pass
            self.cancel_event.set()
            try:
                self.runner.ssh.cancel_all_running()
            except Exception as e:
                logger.error(f"[Dataset] Falha ao cancelar SSHs ativos: {e}")

    def _run_safe(self, experiment_yaml: str, out_dir: str):
        status = "ok"
        try:
            self.progress.emit("[Dataset] Preparando orquestrador…")
            self.runner.run_from_yaml(experiment_yaml, out_dir, cancel_event=self.cancel_event)
            self.progress.emit("[Dataset] Execução concluída, preparando artefatos…")
        except CancelledError:
            status = "aborted"
            logger.warning("[Dataset] Execução abortada pelo usuário.")
            try:
                self.progress.emit("[Dataset] Execução abortada pelo usuário.")
            except Exception:
                pass
        except Exception as e:
            status = "error"
            logger.error(f"[Dataset] Erro na execução: {e}")
            try:
                self.progress.emit(f"[Dataset] Erro na execução: {e}")
            except Exception:
                pass
        finally:
            self.finished.emit(status)
