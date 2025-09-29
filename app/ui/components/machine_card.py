# app/ui/components/machine_card.py
# -*- coding: utf-8 -*-
"""
MachineCard (Matrix Edition 2.2)
- Cabeçalho: chevron, ícone (pequeno) da máquina, nome, menu Ações
- Conteúdo colapsável com animação (inicia minimizado)
- Avatar grande sincronizado com role/status
- Pills informativas com elipse + tooltip
"""
import logging
from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve, QTimer
from PySide6.QtGui import QAction, QFontMetrics, QPixmap, QColor
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QWidget, QToolButton, QMenu,
    QSizePolicy
)

from app.ui.components.anim_utils import HoverGlowFilter, crossfade_label_pixmap
from app.ui.components.machine_avatar import MachineAvatarExt, ICONS
from app.ui.components.flow_layout import FlowLayout
from app.ui.components.info_pills import InfoPill

logger = logging.getLogger("[MachineCardExt]")
if not hasattr(logger, "warn"):
    logger.warn = logger.warning


class _CollapsibleArea(QFrame):
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

        self._finished_slot = None
        self._expanded = False
        try:
            self._content.setVisible(False)
            self.setMaximumHeight(0)
        except Exception as e:
            logger.error(f"[Collapsible] init (collapse) falhou: {e}")

    def _disconnect_finished(self):
        try:
            if self._finished_slot is not None:
                self._anim.finished.disconnect(self._finished_slot)
                self._finished_slot = None
        except Exception:
            pass

    def setExpanded(self, on: bool):
        try:
            if on == self._expanded:
                return
            self._expanded = on

            self._content.setVisible(True)
            h = max(0, self._content.sizeHint().height())
            start = self.maximumHeight()
            end = h if on else 0
            if not on and start <= 0:
                start = h
            if on and start <= 0:
                start = 0

            self._anim.stop()
            self._anim.setStartValue(max(0, start))
            self._anim.setEndValue(max(0, end))

            self._disconnect_finished()

            if not on:
                def _hide():
                    try:
                        self._content.setVisible(False)
                    except Exception:
                        pass
                self._anim.finished.connect(_hide)
                self._finished_slot = _hide

            self._anim.start()
        except Exception as e:
            logger.error(f"[Collapsible] setExpanded: {e}")

    def isExpanded(self) -> bool:
        return self._expanded


class MachineCardWidgetExt(QFrame):
    ICON_PX_SMALL = 22  # ícone do cabeçalho

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        try:
            self.setObjectName("MachineCard")
            self.setProperty("machine", name)
            self.setFrameShape(QFrame.NoFrame)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

            self._role = "general"
            self._vis = "offline"

            root = QVBoxLayout(self)
            root.setAlignment(Qt.AlignTop)
            root.setContentsMargins(12, 12, 12, 12)
            root.setSpacing(10)

            # ---------- HEADER ----------
            header = QHBoxLayout()
            header.setAlignment(Qt.AlignLeft)
            header.setContentsMargins(0, 0, 0, 0)


            self._chevron = QLabel("▸")
            self._chevron.setObjectName("chevron")
            header.addWidget(self._chevron)

            self.header_icon = QLabel()
            self.header_icon.setObjectName("headerIcon")
            self.header_icon.setFixedSize(self.ICON_PX_SMALL, self.ICON_PX_SMALL)
            self.header_icon.setToolTip("estado da máquina")

            try:
                self._icon_hover = HoverGlowFilter(
                    self.header_icon,
                    online_color=QColor(0, 255, 180, 130),
                    offline_color=QColor(255, 80, 80, 130),
                    radius=18,
                    duration=140
                )
            except Exception as e:
                logger.error(f"[MachineCard] hover glow init: {e}")

            header.addWidget(self.header_icon)

            self.title = QLabel(name)
            self.title.setObjectName("machineTitle")
            header.addWidget(self.title)

            header.addStretch(1)

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

            try:
                self._vis = "offline"
                self._update_actions_enabled()
            except Exception as e:
                logger.error(f"[MachineCard] init action states: {e}")

            header.addWidget(self.menu_btn)

            header_frame = QWidget()
            header_frame.setLayout(header)
            header_frame.mousePressEvent = self._toggle_collapsed
            root.addWidget(header_frame)

            # ---------- CONTENT ----------
            content = QWidget()
            cv = QVBoxLayout(content)
            cv.setAlignment(Qt.AlignTop)
            cv.setContentsMargins(0, 0, 0, 0)

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

            # Avatar grande
            self.avatar = MachineAvatarExt(self)
            self.avatar.setObjectName("machineAvatar")

            try:
                nm = (name or "").strip().lower()
                self._role = "attacker" if nm == "attacker" else ("sensor" if ("sensor" in nm) else "general")
                self.avatar.setRole(self._role)
            except Exception as e:
                logger.error(f"[MachineCard] setRole inicial falhou: {e}")

            cv.addWidget(self.avatar)

            self._risk_badge = QLabel("")
            self._risk_badge.setObjectName("riskBadge")
            self._risk_badge.setVisible(False)
            cv.addWidget(self._risk_badge, alignment=Qt.AlignRight)

            self._collapsible = _CollapsibleArea(content, self)
            self._collapsible.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            root.addWidget(self._collapsible)

            def _fix_initial_height():
                try:
                    h0 = max(0, content.sizeHint().height())
                    if self._collapsible.isExpanded():
                        self._collapsible.setMaximumHeight(h0)
                        content.setVisible(True)
                        self._chevron.setText("▾")
                    else:
                        self._collapsible.setMaximumHeight(0)
                        content.setVisible(False)
                        self._chevron.setText("▸")
                    self._refresh_header_icon()
                except Exception as e:
                    logger.error(f"[MachineCard] falha ao ajustar altura inicial: {e}")

            QTimer.singleShot(0, _fix_initial_height)
        except Exception as e:
            logger.error(f"[MachineCard] Erro ao construir card '{name}': {e}")

    # -------- UX ----------
    def _update_actions_enabled(self):
        try:
            online = (getattr(self, "_vis", "offline") == "online")
            # Ações que exigem VM online
            if hasattr(self, "act_ssh"):
                self.act_ssh.setEnabled(online)
                self.act_ssh.setToolTip("Abrir SSH (requer ONLINE)" if not online else "Abrir SSH")
            if hasattr(self, "act_restart"):
                self.act_restart.setEnabled(online)
                self.act_restart.setToolTip("Restart requer máquina ONLINE" if not online else "Restart")
            if hasattr(self, "act_halt"):
                self.act_halt.setEnabled(online)
                self.act_halt.setToolTip("Halt requer máquina ONLINE" if not online else "Halt")
            # Ações sempre permitidas
            if hasattr(self, "act_up"):
                self.act_up.setEnabled(True)
            if hasattr(self, "act_status"):
                self.act_status.setEnabled(True)
            if hasattr(self, "act_destroy"):
                # Destroy pode ser permitido sempre; ajuste conforme sua política
                self.act_destroy.setEnabled(True)
        except Exception as e:
            logger.error(f"[MachineCard] _update_actions_enabled: {e}")


    def _toggle_collapsed(self, _event):
        try:
            new_state = not self._collapsible.isExpanded()
            self._collapsible.setExpanded(new_state)
            self._chevron.setText("▾" if new_state else "▸")
        except Exception as e:
            logger.error(f"[MachineCard] toggle: {e}")

    # -------- Interno ----------
    def _refresh_header_icon(self):
        """Atualiza o ícone pequeno do header com crossfade e property de estado."""
        try:
            pm: QPixmap = ICONS.get_icon(self._role, self._vis, self.ICON_PX_SMALL)
            if not pm.isNull():
                self.header_icon.setProperty("vis", self._vis)
                crossfade_label_pixmap(self.header_icon, pm, duration=180)
                self.header_icon.setToolTip(f"{self._role} | {self._vis}")
            else:
                self.header_icon.clear()
        except Exception as e:
            logger.error(f"[MachineCard] _refresh_header_icon: {e}")

    # -------- API pública extra ----------
    def set_machine_role(self, role: str):
        """Altera dinamicamente o role do avatar e do header icon: attacker | sensor | general."""
        try:
            role = (role or "").lower().strip()
            if role not in ("attacker", "sensor", "general"):
                role = "general"
            self._role = role
            self.avatar.setRole(role)
            self._refresh_header_icon()
            logger.info(f"[MachineCard] role alterado para {self._role}")
        except Exception as e:
            logger.error(f"[MachineCard] set_machine_role falhou: {e}")

    # -------- API compat/extend ----------
    def set_status(self, status: str):
        """
        Recebe 'running' | 'stopped' | outros.
        Atualiza statusDot e avatar (online/offline) e o ícone do cabeçalho.
        """
        try:
            st = status if status in ("running", "stopped") else "unknown"

            self._vis = "online" if st == "running" else "offline"
            try:
                self.avatar.setStatus(self._vis)
            except Exception as e:
                logger.error(f"[MachineCard] avatar.setStatus falhou: {e}")

            self._refresh_header_icon()
            self._update_actions_enabled()
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
        try:
            self.set_pill_values(os_text, host_endpoint, guest_ip)
        except Exception as e:
            logger.error(f"[MachineCard] _set_card_info: {e}")

    def set_pill_values(self, os_text: str, host: str, guest: str):
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
