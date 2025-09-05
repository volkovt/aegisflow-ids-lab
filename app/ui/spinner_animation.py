import logging

from PySide6.QtCore import QTimer

class _SpinnerAnimator:
    """
    Anima '⠋⠙⠹…' no texto de um QPushButton/QLabel durante execução em background.
    """
    FRAMES = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]

    def __init__(self, widget, base_text: str):
        self.widget = widget
        self.base_text = base_text
        self._timer = QTimer()
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._tick)
        self._i = 0
        self._logger = logging.getLogger("[Spinner]")

    def start(self):
        try:
            if hasattr(self.widget, "setProperty"):
                self.widget.setProperty("loading", True)
                try:
                    self.widget.setEnabled(False)
                except Exception:
                    pass
                self.widget.style().unpolish(self.widget)
                self.widget.style().polish(self.widget)
            self._timer.start()
        except Exception as e:
            self._logger.warning(f"[Spinner] start falhou: {e}")

    def stop(self, final_text: str | None = None):
        try:
            self._timer.stop()
            if hasattr(self.widget, "setProperty"):
                self.widget.setProperty("loading", False)
                try:
                    self.widget.setEnabled(True)
                except Exception:
                    pass
                self.widget.style().unpolish(self.widget)
                self.widget.style().polish(self.widget)
            if final_text is not None:
                self._set_text(final_text)
            else:
                self._set_text(self.base_text)
        except Exception as e:
            self._logger.warning(f"[Spinner] stop falhou: {e}")

    def _tick(self):
        try:
            frame = _SpinnerAnimator.FRAMES[self._i % len(_SpinnerAnimator.FRAMES)]
            self._i += 1
            self._set_text(f"{self.base_text} {frame}")
        except Exception as e:
            self._logger.warning(f"[Spinner] tick falhou: {e}")

    def _set_text(self, text: str):
        # QLabel e QPushButton ambos possuem setText
        try:
            self.widget.setText(text)
        except Exception as e:
            self._logger.warning(f"[Spinner] set_text falhou: {e}")
