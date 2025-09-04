from PySide6.QtGui import QGuiApplication, QFontMetrics
from PySide6.QtWidgets import QToolTip, QSizePolicy
from PySide6.QtWidgets import QPushButton, QWidget
from PySide6.QtCore import Qt

import logging

_ui_logger = logging.getLogger("[InfoPill]")

class InfoPill(QPushButton):
    """
    Botão estilo 'pill' com elipse automática, tooltip completo e clique para copiar.
    """
    def __init__(self, label_prefix: str, value: str = "—", kind: str = "default", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("InfoPill")
        self.setProperty("kind", kind)  # so | host | guest | default
        self._prefix = label_prefix
        self._full_value = value
        self._elided = ""
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMinimumHeight(28)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._update_text()
        self._update_tooltip()
        self.clicked.connect(self._copy_value)

    def setValue(self, value: str):
        self._full_value = value or "—"
        self._update_text()
        self._update_tooltip()

    def value(self) -> str:
        return self._full_value

    def _update_tooltip(self):
        self.setToolTip(f"{self._prefix}: {self._full_value}")

    def _update_text(self):
        try:
            fm = QFontMetrics(self.font())
            # Reservar alguns chars para o prefixo “SO:” etc.
            base = f"{self._prefix}: "
            # Estimativa do espaço disponível
            avail = max(60, self.width() - fm.horizontalAdvance(base) - 16)
            elided = fm.elidedText(self._full_value, Qt.ElideMiddle, avail)
            self._elided = elided
            self.setText(base + elided)
        except Exception as e:
            _ui_logger.warning(f"[InfoPill] falha ao elidir texto: {e}")
            self.setText(f"{self._prefix}: {self._full_value}")

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._update_text()

    def _copy_value(self):
        try:
            cb = QGuiApplication.clipboard()
            cb.setText(self._full_value)
            QToolTip.showText(self.mapToGlobal(self.rect().center()), "Copiado ✓", self, self.rect(), 1200)
            _ui_logger.info(f"[InfoPill] valor copiado: {self._prefix}={self._full_value}")
        except Exception as e:
            _ui_logger.error(f"[InfoPill] erro ao copiar: {e}")
