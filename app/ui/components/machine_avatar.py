# app/ui/components/machine_avatar.py
import logging
from PySide6.QtCore import QSize, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QBrush
from PySide6.QtWidgets import QStyleOption, QWidget, QStyle, QSizePolicy

logger = logging.getLogger("[MachineAvatarExt]")
if not hasattr(logger, "warn"):
    logger.warn = logger.warning

class MachineAvatarExt(QWidget):
    """
    Desenha um 'computador' com glow verde + scanlines (estilo Matrix)
    mantendo proporção 16:9 para a tela, evitando distorção.
    """
    ASPECT = 16 / 9

    def __init__(self, parent=None):
        super().__init__(parent)
        try:
            self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        except Exception as e:
            logger.error(f"[MachineAvatar] sizePolicy: {e}")

    def sizeHint(self):
        return QSize(320, 180)  # 16:9

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w: int) -> int:
        try:
            return max(120, int(w / self.ASPECT))
        except Exception:
            return 180

    def _fit_rect_16x9(self, rect) -> QRectF:
        try:
            avail_w = rect.width()
            avail_h = rect.height()
            target_w = min(avail_w, int(avail_h * self.ASPECT))
            if target_w / self.ASPECT > avail_h:
                target_w = int(avail_h * self.ASPECT)
                target_h = avail_h
            else:
                target_h = int(target_w / self.ASPECT)
            x = rect.left() + (avail_w - target_w) / 2
            y = rect.top() + (avail_h - target_h) / 2
            return QRectF(x, y, target_w, target_h)
        except Exception as e:
            logger.warn(f"[MachineAvatar] _fit_rect_16x9: {e}")
            return QRectF(rect)

    def paintEvent(self, e):
        try:
            opt = QStyleOption()
            opt.initFrom(self)
            p = QPainter(self)
            self.style().drawPrimitive(QStyle.PE_Widget, opt, p, self)

            outer = self.rect().adjusted(10, 10, -10, -10)
            screen = self._fit_rect_16x9(outer.adjusted(0, 0, 0, -24))

            base = QColor(0, 255, 170)
            glow = QColor(0, 255, 170, 70)

            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(QPen(base, 2))
            p.setBrush(QBrush(QColor(10, 30, 20)))
            p.drawRoundedRect(screen, 10, 10)

            p.setBrush(QBrush(glow))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(screen.adjusted(-6, -6, 6, 6), 14, 14)

            base_rect = QRectF(outer.left() + outer.width() * 0.15, outer.bottom() - 18, outer.width() * 0.7, 10)
            p.setBrush(QBrush(QColor(12, 40, 28)))
            p.setPen(QPen(base, 1.5))
            p.drawRoundedRect(base_rect, 4, 4)

            p.setPen(QPen(QColor(0, 255, 170, 60), 1))
            y0 = int(screen.top()) + 8
            y1 = int(screen.bottom()) - 8
            for y in range(y0, y1, 6):
                p.drawLine(int(screen.left()) + 8, y, int(screen.right()) - 8, y)
        except Exception as ex:
            logger.warn(f"[MachineAvatar] paintEvent falhou: {ex}")
