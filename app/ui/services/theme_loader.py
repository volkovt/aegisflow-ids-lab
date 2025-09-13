from __future__ import annotations
from pathlib import Path
import logging

from PySide6.QtWidgets import QWidget


def load_theme(widget: QWidget, theme_path: Path, *, logger: logging.Logger):
    try:
        qss = Path(theme_path).read_text(encoding="utf-8")
        widget.setStyleSheet(qss)
    except Exception as e:
        try:
            logger.warning(f"[UI] Falha ao carregar tema: {e}")
        except Exception:
            pass
