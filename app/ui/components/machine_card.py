# app/ui/components/machine_card.py
# -*- coding: utf-8 -*-
"""
MachineCard (Matrix Edition 2.0)
- Bloco expansível (colapsável) por máquina, empilhado verticalmente
- Menu único de ações (Up/Status/Restart/Halt/Destroy/SSH)
- Pills SO/Host/Guest com elipse e tooltip (sem cortes)
- Badge de risco p/ modelo de anomalias (mantido)
"""
import logging
from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve, QTimer
from PySide6.QtGui import QAction, QFontMetrics
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QWidget, QToolButton, QMenu,
    QSizePolicy
)

from app.ui.components.machine_avatar import MachineAvatarExt
from app.ui.components.flow_layout import FlowLayout
from app.ui.components.info_pills import InfoPill

logger = logging.getLogger("[MachineCardExt]")
if not hasattr(logger, "warn"):
    logger.warn = logger.warning

class _CollapsibleArea(QFrame):
    """Conteúdo colapsável com animação vertical simples."""
    def __init__(self, content: QWidget, parent=None):
        super().__init__(parent)
        self._content = content
        self._content.setParent(self)
        v = QVBoxLayout(self)
        v.setAlignment(Qt.AlignTop)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(content)
        self._anim = QPropertyAnimation(self, b"maximumHeight", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.InOutCubic)
        self._expanded = True

    def setExpanded(self, on: bool):
        try:
            if on == self._expanded:
                return
            self._expanded = on
            self._content.setVisible(True)
            start = self.maximumHeight() if self.maximumHeight() > 0 else self.sizeHint().height()
            end = self._content.sizeHint().height() if on else 0
            self._anim.stop()
            self._anim.setStartValue(max(0, start))
            self._anim.setEndValue(max(0, end))
            self._anim.start()
            if not on:
                self._anim.finished.connect(lambda: self._content.setVisible(False))
            else:
                try:
                    self._anim.finished.disconnect()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[Collapsible] setExpanded: {e}")
            self._content.setVisible(on)

    def isExpanded(self) -> bool:
        return self._expanded


class MachineCardWidgetExt(QFrame):
    """
    Atributos compatíveis principais:
    - title (QLabel)
    - statusDot (QLabel c/ property 'status': running|stopped|unknown)
    - pills['so'|'host'|'guest']  (InfoPill)
    - set_status(status)
    - set_risk_score(score, threshold)

    Novos:
    - menu_btn (QToolButton)  -> botão "Ações"
    - act_up/act_status/act_restart/act_halt/act_destroy/act_ssh (QAction)
    - set_pill_values(os_text, host, guest) com elipse + tooltip
    """
    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        try:
            self.setObjectName("MachineCard")
            self.setProperty("machine", name)
            self.setFrameShape(QFrame.StyledPanel)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

            root = QVBoxLayout(self)
            root.setAlignment(Qt.AlignTop)
            root.setContentsMargins(12, 12, 12, 12)
            root.setSpacing(10)

            header = QHBoxLayout()
            header.setAlignment(Qt.AlignLeft)
            header.setContentsMargins(0, 0, 0, 0)

            self._chevron = QLabel("▾")  # alterna para ▸ quando colapsado
            self._chevron.setObjectName("chevron")
            header.addWidget(self._chevron)

            self.title = QLabel(name)
            self.title.setObjectName("machineTitle")
            header.addWidget(self.title)

            header.addStretch(1)

            self.statusDot = QLabel("●")
            self.statusDot.setObjectName("statusDot")
            self.statusDot.setProperty("status", "unknown")
            header.addWidget(self.statusDot)

            self.menu_btn = QToolButton()
            self.menu_btn.setText("Ações")
            self.menu_btn.setPopupMode(QToolButton.InstantPopup)
            self.menu_btn.setObjectName("actionMenuBtn")

            menu = QMenu(self)
            self.act_up = QAction("Up", self)
            self.act_status = QAction("Status", self)
            self.act_restart = QAction("Restart", self)
            self.act_halt = QAction("Halt", self)
            self.act_destroy = QAction("Destroy", self)
            self.act_ssh = QAction("SSH", self)

            for a in (self.act_up, self.act_status, self.act_restart, self.act_halt, self.act_destroy, self.act_ssh):
                menu.addAction(a)
            self.menu_btn.setMenu(menu)
            header.addWidget(self.menu_btn)

            header_frame = QWidget()
            header_frame.setLayout(header)
            header_frame.mousePressEvent = self._toggle_collapsed
            root.addWidget(header_frame)

            content = QWidget()
            cv = QVBoxLayout(content)
            cv.setAlignment(Qt.AlignTop)
            cv.setContentsMargins(0, 0, 0, 0)
            #cv.setSpacing(8)

            pill_container = QWidget()
            pill_flow = FlowLayout(pill_container, hspacing=10, vspacing=10, alignment=Qt.AlignHCenter)
            self.pills = {
                "so": InfoPill("SO", "—", kind="so", parent=pill_container),
                "host": InfoPill("Host", "—", kind="host", parent=pill_container),
                "guest": InfoPill("Guest", "—", kind="guest", parent=pill_container),
            }
            pill_flow.addWidget(self.pills["so"])
            pill_flow.addWidget(self.pills["host"])
            pill_flow.addWidget(self.pills["guest"])
            cv.addWidget(pill_container)

            self.avatar = MachineAvatarExt(self)
            self.avatar.setObjectName("machineAvatar")
            cv.addWidget(self.avatar)

            self._risk_badge = QLabel("")
            self._risk_badge.setObjectName("riskBadge")
            self._risk_badge.setVisible(False)
            cv.addWidget(self._risk_badge, alignment=Qt.AlignRight)

            self._collapsible = _CollapsibleArea(content, self)
            self._collapsible.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            root.addWidget(self._collapsible)

            try:
                def _fix_initial_height():
                    try:
                        h0 = max(0, content.sizeHint().height())
                        self._collapsible.setMaximumHeight(h0)
                        logger.info(f"[MachineCard] altura inicial ajustada: {h0}px")
                    except Exception as e:
                        logger.error(f"[MachineCard] falha ao ajustar altura inicial: {e}")

                QTimer.singleShot(0, _fix_initial_height)
            except Exception as e:
                logger.error(f"[MachineCard] erro no setup de altura inicial: {e}")

            logger.info(f"[MachineCard] Bloco criado: {name}")
        except Exception as e:
            logger.error(f"[MachineCard] Erro ao construir card '{name}': {e}")

    # -------- UX ----------
    def _toggle_collapsed(self, _event):
        try:
            new_state = not self._collapsible.isExpanded()
            self._collapsible.setExpanded(new_state)
            self._chevron.setText("▾" if new_state else "▸")
        except Exception as e:
            logger.error(f"[MachineCard] toggle: {e}")

    # -------- API compat/extend ----------
    def set_status(self, status: str):
        try:
            st = status if status in ("running", "stopped") else "unknown"
            self.statusDot.setProperty("status", st)
            self.statusDot.style().unpolish(self.statusDot)
            self.statusDot.style().polish(self.statusDot)
        except Exception as e:
            logger.error(f"[MachineCard] set_status: {e}")

    def set_risk_score(self, score: float | None, threshold: float = 0.7):
        try:
            if score is None:
                self._risk_badge.setVisible(False)
                return
            self._risk_badge.setText(f"risk {score:.2f}")
            self._risk_badge.setVisible(True)
            self._risk_badge.setProperty("level", "high" if score >= threshold else "low")
            self._risk_badge.style().unpolish(self._risk_badge)
            self._risk_badge.style().polish(self._risk_badge)
        except Exception as e:
            logger.error(f"[MachineCard] set_risk_score: {e}")

    def _set_card_info(self, os_text: str, host_endpoint: str, guest_ip: str):
        """
        API compatível com chamadores antigos. Encapsula a atualização
        das pills no próprio card (elipse + tooltip).
        """
        try:
            self.set_pill_values(os_text, host_endpoint, guest_ip)
            logger.info("[MachineCard] _set_card_info aplicado.")
        except Exception as e:
            logger.error(f"[MachineCard] _set_card_info: {e}")

    def set_pill_values(self, os_text: str, host: str, guest: str):
        """Aplica elipse e tooltip – evita cortes e mantém info completa acessível."""
        try:
            def _elide(pill: InfoPill, value: str):
                if value is None:
                    value = "—"
                fm = QFontMetrics(pill.font())
                clipped = fm.elidedText(value, Qt.ElideRight, 400)
                pill.setValue(clipped)
                pill.setToolTip(value)

            _elide(self.pills["so"], os_text)
            _elide(self.pills["host"], host)
            _elide(self.pills["guest"], guest)
        except Exception as e:
            logger.error(f"[MachineCard] set_pill_values: {e}")
