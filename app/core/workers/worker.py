from PySide6.QtCore import Signal, QThread


class Worker(QThread):
    line = Signal(str)
    done = Signal()
    error = Signal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            for ln in self.fn(*self.args, **self.kwargs):
                self.line.emit(ln)
            self.done.emit()
        except Exception as e:
            self.error.emit(str(e))
