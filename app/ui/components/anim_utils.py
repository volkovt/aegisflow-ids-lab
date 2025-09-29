# -*- coding: utf-8 -*-
import logging
from PySide6.QtCore import Qt, QObject, QEvent, QPropertyAnimation, QEasingCurve, Property, QRectF, QPointF
from PySide6.QtGui import QColor, QPixmap, QPainter, QLinearGradient
from PySide6.QtWidgets import QLabel, QGraphicsOpacityEffect, QGraphicsDropShadowEffect, QWidget

logger = logging.getLogger("[AnimUtils]")
if not hasattr(logger, "warn"):
    logger.warn = logger.warning

def _pick_state_color(state: str, online: QColor, offline: QColor) -> QColor:
    s = (state or "").lower()
    return online if s in ("on", "online", "ok", "ready") else offline


def crossfade_label_pixmap(label: QLabel, new_pm: QPixmap, duration: int = 180):
    """Faz fade do pixmap anterior -> novo, sem flicker."""
    try:
        if new_pm is None or new_pm.isNull():
            return
        old = label.pixmap()
        if old is None or old.isNull():
            label.setPixmap(new_pm)
            return

        overlay = QLabel(label.parent())
        overlay.setObjectName("IconSwapOverlay")
        overlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        overlay.setGeometry(label.geometry())
        overlay.setPixmap(old)
        overlay.raise_()

        label.setPixmap(new_pm)

        eff = QGraphicsOpacityEffect(overlay)
        overlay.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", overlay)
        anim.setDuration(max(80, int(duration)))
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)

        def _cleanup():
            try:
                overlay.deleteLater()
            except Exception:
                pass

        anim.finished.connect(_cleanup)
        anim.start(QPropertyAnimation.DeleteWhenStopped)
    except Exception as e:
        logger.error(f"[crossfade_label_pixmap] {e}")
        try:
            label.setPixmap(new_pm)
        except Exception:
            pass


class HoverGlowFilter(QObject):
    """Anima um 'glow' com DropShadow no hover do QLabel (ícone)."""
    def __init__(self, label: QLabel,
                 online_color: QColor = QColor(0, 255, 180, 130),
                 offline_color: QColor = QColor(255, 80, 80, 130),
                 radius: int = 18, duration: int = 140):
        super().__init__(label)
        self.label = label
        self.online_color = online_color
        self.offline_color = offline_color
        self.radius = int(radius)
        self.duration = int(duration)
        self._eff = QGraphicsDropShadowEffect(label)
        try:
            self._eff.setBlurRadius(0)
            self._eff.setOffset(0, 0)
            self._eff.setColor(self.offline_color)
            label.setGraphicsEffect(self._eff)
            label.setAttribute(Qt.WA_Hover, True)
            label.setMouseTracking(True)
            label.installEventFilter(self)
        except Exception as e:
            logger.error(f"[HoverGlowFilter] install: {e}")

    def _pick_color(self):
        try:
            vis = (self.label.property("vis") or "offline").lower()
            return self.online_color if vis == "online" else self.offline_color
        except Exception:
            return self.offline_color

    def _animate_radius(self, start: float, end: float):
        try:
            self._eff.setColor(self._pick_color())
            anim = QPropertyAnimation(self._eff, b"blurRadius", self.label)
            anim.setDuration(self.duration)
            anim.setStartValue(float(start))
            anim.setEndValue(float(end))
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.start(QPropertyAnimation.DeleteWhenStopped)
        except Exception as e:
            logger.error(f"[HoverGlowFilter] animate: {e}")

    def eventFilter(self, obj, ev):
        try:
            if obj is self.label:
                if ev.type() == QEvent.HoverEnter:
                    self._animate_radius(self._eff.blurRadius(), self.radius)
                elif ev.type() == QEvent.HoverLeave:
                    self._animate_radius(self._eff.blurRadius(), 0.0)
        except Exception as e:
            logger.error(f"[HoverGlowFilter] event: {e}")
        return False


class ShineSweepOverlay(QWidget):
    """Overlay translúcido com uma faixa de brilho que cruza o avatar."""
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._pos = -0.4  # -0.4 (fora à esquerda) -> 1.4 (fora à direita)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.hide()

    def sizeHint(self):
        return self.parent().size()

    def setProgress(self, v: float):
        self._pos = float(v)
        self.update()

    def progress(self) -> float:
        return self._pos

    progress = Property(float, fget=progress, fset=setProgress)

    def paintEvent(self, ev):
        try:
            if not self.isVisible():
                return
            w = self.width()
            h = self.height()
            if w <= 0 or h <= 0:
                return

            p = QPainter(self)
            p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)

            # faixa diagonal
            band_w = max(32, int(w * 0.25))
            x = int((self._pos - 0.2) * w)
            rect = QRectF(x, -h * 0.2, band_w, h * 1.4)

            grad = QLinearGradient(rect.topLeft(), rect.topRight())
            grad.setColorAt(0.0, QColor(255, 255, 255, 0))
            grad.setColorAt(0.48, QColor(255, 255, 255, 60))
            grad.setColorAt(0.52, QColor(255, 255, 255, 100))
            grad.setColorAt(0.58, QColor(255, 255, 255, 60))
            grad.setColorAt(1.0, QColor(255, 255, 255, 0))

            p.translate(QPointF(0, 0))
            p.rotate(-18)  # leve diagonal
            p.fillRect(rect, grad)
            p.end()
        except Exception as e:
            logger.error(f"[ShineSweepOverlay] paint: {e}")


class AvatarHoverAnimator(QObject):
    """
    Animações de hover para um QLabel de avatar:
      - Glow forte (DropShadow blurRadius)
      - Sheen (varredura de brilho)
    """
    def __init__(self, label: QLabel,
                 online_color: QColor = QColor(0, 255, 180, 180),
                 offline_color: QColor = QColor(255, 80, 80, 180),
                 max_glow: int = 42, duration: int = 180):
        super().__init__(label)
        self.label = label
        self.online_color = online_color
        self.offline_color = offline_color
        self.max_glow = int(max_glow)
        self.duration = int(duration)

        try:
            # efeito de glow
            self._shadow = QGraphicsDropShadowEffect(label)
            self._shadow.setBlurRadius(0)
            self._shadow.setOffset(0, 0)
            self._shadow.setColor(self.offline_color)
            label.setGraphicsEffect(self._shadow)

            # overlay de brilho
            self._sheen = ShineSweepOverlay(label)
            self._sheen.setGeometry(label.rect())
            self._sheen.raise_()

            # interações
            label.setAttribute(Qt.WA_Hover, True)
            label.setMouseTracking(True)
            label.installEventFilter(self)
        except Exception as e:
            logger.error(f"[AvatarHoverAnimator] init: {e}")

    # --- util ---
    def _color_for_label(self) -> QColor:
        st = (self.label.property("vis") or self.label.property("status") or "offline")
        return _pick_state_color(str(st), self.online_color, self.offline_color)

    def _animate_glow(self, start: float, end: float, ease=QEasingCurve.OutCubic, ms: int | None = None):
        try:
            self._shadow.setColor(self._color_for_label())
            anim = QPropertyAnimation(self._shadow, b"blurRadius", self.label)
            anim.setDuration(ms if ms is not None else self.duration)
            anim.setStartValue(float(start))
            anim.setEndValue(float(end))
            anim.setEasingCurve(ease)
            anim.start(QPropertyAnimation.DeleteWhenStopped)
        except Exception as e:
            logger.error(f"[AvatarHoverAnimator] glow: {e}")

    def _run_sheen(self):
        try:
            self._sheen.setGeometry(self.label.rect())
            self._sheen.show()
            anim = QPropertyAnimation(self._sheen, b"progress", self)
            anim.setDuration(max(350, int(self.duration * 2)))
            anim.setStartValue(-0.4)
            anim.setEndValue(1.4)
            anim.setEasingCurve(QEasingCurve.OutCubic)

            def _hide():
                try:
                    self._sheen.hide()
                except Exception:
                    pass

            anim.finished.connect(_hide)
            anim.start(QPropertyAnimation.DeleteWhenStopped)
        except Exception as e:
            logger.error(f"[AvatarHoverAnimator] sheen: {e}")

    # --- event filter ---
    def eventFilter(self, obj, ev):
        try:
            if obj is self.label:
                if ev.type() == QEvent.Resize:
                    self._sheen.setGeometry(self.label.rect())
                elif ev.type() == QEvent.HoverEnter:
                    self._animate_glow(self._shadow.blurRadius(), self.max_glow, QEasingCurve.OutBack, ms=self.duration + 60)
                    self._run_sheen()
                elif ev.type() == QEvent.HoverLeave:
                    self._animate_glow(self._shadow.blurRadius(), 0.0, QEasingCurve.OutCubic, ms=self.duration)
        except Exception as e:
            logger.error(f"[AvatarHoverAnimator] event: {e}")
        return False
