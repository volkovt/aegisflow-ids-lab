# -*- coding: utf-8 -*-
import logging
from pathlib import Path

from PySide6.QtCore import QTimer

from app.core.logger_setup import setup_logger

logger = setup_logger(Path('.logs'), name="[Spinner]")
if not hasattr(logger, "warn"):
    logger.warn = logger.warning


class _MiniSpinner:
    FRAMES = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]

    def __init__(self, label, base="Executando"):
        self.label = label
        self.base = base
        self.i = 0
        self.timer = QTimer(label)
        self.timer.timeout.connect(self.tick)

    def start(self, text=None):
        if text:
            self.base = text
        self.timer.start(90)

    def stop(self, text=None):
        self.timer.stop()
        if text is not None and hasattr(self.label, "setText"):
            self.label.setText(text)

    def tick(self):
        try:
            f = _MiniSpinner.FRAMES[self.i % len(_MiniSpinner.FRAMES)]
            self.i += 1
            if hasattr(self.label, "setText"):
                self.label.setText(f"{self.base} {f}")
        except Exception as e:
            logger.error(f"[Spinner] tick: {e}")
