import logging
from PySide6.QtCore import QObject, QThread, Signal, Qt, QTimer

logger = logging.getLogger("[ColetaSO]")
if not hasattr(logger, "warn"):
    logger.warn = logger.warning

class OSWorker(QObject):
    done = Signal(str, str)  # (vm_name, os_text)

    def __init__(self, ssh_manager, vm_name, timeout=10):
        super().__init__()
        self.ssh = ssh_manager
        self.vm = vm_name
        self.timeout = timeout

    def run(self):
        try:
            logger.info(f"[ColetaSO] Iniciando probe em {self.vm}...")
            os_text = self.ssh.probe_os(self.vm, timeout=self.timeout)
            logger.info(f"[ColetaSO] finalizado {self.vm}: {os_text}")
            if not isinstance(os_text, str):
                os_text = os_text.get("text", "—")
            self.done.emit(self.vm, (os_text or "—").strip())
        except Exception as e:
            logger.warn(f"[ColetaSO] Erro em {self.vm}: {e}")
            self.done.emit(self.vm, "—")

def refresh_os_async(self, vm_name: str):
    """
    self: MainWindow (QObject no thread da UI)
    """
    try:
        worker = OSWorker(self.ssh, vm_name, timeout=8)
        th = QThread()
        th.setObjectName(f"osProbe-{vm_name}")
        worker.moveToThread(th)

        th.started.connect(worker.run)
        worker.done.connect(self.osTextArrived.emit, Qt.QueuedConnection)

        worker.done.connect(th.quit)
        worker.done.connect(worker.deleteLater)
        th.finished.connect(lambda: self._on_os_thread_finished(vm_name, th))

        th.start(QThread.LowPriority)

        if hasattr(self, "_os_threads"):
            self._os_threads[vm_name] = th

        return th
    except Exception as e:
        logger.error(f"[ColetaSO] Falha ao iniciar thread para {vm_name}: {e}")
        try:
            QTimer.singleShot(0, lambda: self.osTextArrived.emit(vm_name, "—"))
        except Exception:
            pass

