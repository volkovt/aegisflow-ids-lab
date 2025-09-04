from PySide6.QtCore import Signal, QThread


class ResultWorker(QThread):
    result = Signal(object)
    error = Signal(str)
    done = Signal()

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            res = self.fn(*self.args, **self.kwargs)
            self.result.emit(res)
            self.done.emit()
        except Exception as e:
            self.error.emit(str(e))