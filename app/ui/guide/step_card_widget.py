# -*- coding: utf-8 -*-
import logging
from PySide6.QtCore import Qt, Signal, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QAction, QFontMetrics, QPixmap, QGuiApplication, QClipboard
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QWidget, QToolButton, QMenu, QPushButton, QPlainTextEdit, QMessageBox, QGraphicsOpacityEffect
)

from app.ui.components.machine_avatar import ICONS  # mesmo provider do MachineCard
from app.ui.guide.spinner import _MiniSpinner
from app.ui.guide.guide_utils import build_copy_payloads

logger = logging.getLogger("[GuideCard]")
if not hasattr(logger, "warn"):
    logger.warn = logger.warning


def role_from_host(host: str) -> str:
    """Normaliza o host/nome para um dos roles aceitos pelos ícones: attacker | sensor | general."""
    try:
        h = (host or "").strip().lower()
        # normalizações comuns
        if h in ("vitima", "ví­tima", "victim", "win", "windows", "server"):
            return "general"
        if h.startswith("att") or "attack" in h or h == "attacker" or "kali" in h:
            return "attacker"
        if h.startswith("sensor") or "sensor" in h or "zeek" in h or "snort" in h:
            return "sensor"
        return "general"
    except Exception as e:
        logger.error(f"[GuideCard] role_from_host: {e}")
        return "general"


class StepCard(QFrame):
    run_clicked = Signal(dict)
    copy_clicked = Signal(str)
    mark_done = Signal(dict)
    ssh_clicked = Signal(str, str)

    def __init__(self, idx: int, step: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("GuideCard")
        self.step = step
        self.idx = idx
        self._spin: _MiniSpinner | None = None
        self._copy_mode = "normal"

        # Estado de ícone idêntico ao MachineCard (provider compartilhado)
        self._vis = "offline"  # online/offline
        self._role = role_from_host(self.step.get("host", ""))

        self._build()
        self._animate_appear()

    # ---------- UI ----------
    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        header = QHBoxLayout(); header.setContentsMargins(0, 0, 0, 0)

        # ícone pequeno — mesma escala do MachineCard (22px)
        self.header_icon = QLabel()
        self.header_icon.setObjectName("GuideHeaderIcon")
        self.header_icon.setFixedSize(22, 22)
        self._refresh_header_icon()
        header.addWidget(self.header_icon)

        title = QLabel(f"{self.idx:02d}. {self.step.get('title','Passo')}")
        title.setObjectName("GuideTitle")
        header.addWidget(title)
        header.addStretch(1)

        self.status = QLabel("A fazer")
        self.status.setObjectName("GuideStatus")
        header.addWidget(self.status)

        layout.addLayout(header)

        desc = QLabel(self.step.get("description", ""))
        desc.setObjectName("GuideDesc")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        meta = QLabel(self._meta_text())
        meta.setObjectName("GuideMeta")
        meta.setWordWrap(True)
        layout.addWidget(meta)

        cmd_to_show = self.step.get("command_normal") or self.step.get("command_b64") or self.step.get("command") or ""
        self.cmd_box = QPlainTextEdit(cmd_to_show)
        self.cmd_box.setReadOnly(True)
        self.cmd_box.setFixedHeight(72)
        self.cmd_box.setObjectName("GuideCmd")
        layout.addWidget(self.cmd_box)

        art = self.step.get("artifacts", [])
        art_label = QLabel("Artefatos esperados: " + (", ".join(art) if art else "—"))
        art_label.setObjectName("GuideArtifacts")
        art_label.setWordWrap(True)
        layout.addWidget(art_label)

        row = QHBoxLayout()
        self.copy_btn = self._build_copy_button(self.step)
        btn_run = QPushButton(f"Rodar no {self.step.get('host', 'guest')}")
        self.btn_ssh = QPushButton(f"SSH em {self.step.get('host','guest')}")
        btn_done = QPushButton("Marcar ✓")

        for b in (btn_run, self.btn_ssh, btn_done):
            b.setObjectName("HoloBtn")

        btn_run.clicked.connect(lambda: self.run_clicked.emit(self.step))
        self.btn_ssh.clicked.connect(lambda: self._emit_ssh(self.step, self.cmd_box.toPlainText()))
        btn_done.clicked.connect(self._on_done)

        row.addWidget(self.copy_btn)
        row.addWidget(btn_run)
        row.addWidget(self.btn_ssh)
        row.addWidget(btn_done)
        row.addStretch(1)
        layout.addLayout(row)

        self._set_status_state("idle")

    def _animate_appear(self):
        try:
            effect = QGraphicsOpacityEffect(self)
            self.setGraphicsEffect(effect)
            anim = QPropertyAnimation(effect, b"opacity", self)
            anim.setDuration(260)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.start(QPropertyAnimation.DeleteWhenStopped)
        except Exception as e:
            logger.error(f"[GuideCard] appear anim: {e}")

    def _meta_text(self):
        tags = self.step.get("tags", [])
        eta = self.step.get("eta", "")
        host = self.step.get("host", "guest")
        parts = []
        if eta: parts.append(f"ETA: {eta}")
        parts.append(f"Host: {host}")
        if tags: parts.append("Tags: " + ", ".join(tags))
        return " • ".join(parts)

    # ---------- Status helpers ----------
    def _set_status_state(self, state: str):
        try:
            self.status.setProperty("state", state)
            self.status.style().unpolish(self.status)
            self.status.style().polish(self.status)
            self.status.update()
        except Exception as e:
            logger.error(f"[GuideCard] status state: {e}")

    def set_running(self):
        try:
            self.status.setText("Executando…")
            self._set_status_state("running")
            # Online durante a execução (usa o mesmo sprite do MachineCard via ICONS)
            self.set_machine_visibility("online")
            if not self._spin:
                self._spin = _MiniSpinner(self.status, "Executando")
            self._spin.start()
        except Exception as e:
            logger.error(f"[GuideCard] set_running: {e}")

    def set_idle(self):
        try:
            if self._spin:
                self._spin.stop("A fazer")
            else:
                self.status.setText("A fazer")
            self._set_status_state("idle")
            self.set_machine_visibility("offline")
        except Exception as e:
            logger.error(f"[GuideCard] set_idle: {e}")

    def set_done(self, ok: bool):
        try:
            self.status.setText("Concluído ✓" if ok else "Finalizado")
            self._set_status_state("done")
            self.set_machine_visibility("offline")
        except Exception as e:
            logger.error(f"[GuideCard] set_done: {e}")

    def set_error(self):
        try:
            self.status.setText("Falhou ✖")
            self._set_status_state("error")
            self.set_machine_visibility("offline")
        except Exception as e:
            logger.error(f"[GuideCard] set_error: {e}")

    # ---------- Copy/SSH ----------
    def _build_copy_button(self, step: dict) -> QToolButton:
        btn = QToolButton(self)
        btn.setObjectName("CopyModeBtn")
        btn.setToolTip("Selecione o modo no menu e clique para copiar")
        btn.setPopupMode(QToolButton.MenuButtonPopup)

        normal, b64, script_text = build_copy_payloads(step)
        modes = [("normal", "Normal", normal), ("b64", "Base64", b64)]
        if script_text:
            modes.append(("script", "Script puro", script_text))

        menu = QMenu(btn)
        actions = {}
        for key, label, _ in modes:
            act = menu.addAction(f"Usar {label}")
            act.setCheckable(True)
            actions[key] = act
        btn.setMenu(menu)

        self._copy_mode = "normal" if "normal" in actions else modes[0][0]
        actions[self._copy_mode].setChecked(True)
        btn.setText(f"Copiar ({dict((k, l) for k, l, _ in modes)[self._copy_mode]})")

        def _select_mode(key: str):
            try:
                self._copy_mode = key
                for k, a in actions.items():
                    a.setChecked(k == key)
                btn.setText(f"Copiar ({dict((k, l) for k, l, _ in modes)[key]})")
                self.status.setText(f"Modo de cópia: {dict((k, l) for k, l, _ in modes)[key]}")
            except Exception as e:
                logger.error(f"[GuideCard] select_mode: {e}")

        if "normal" in actions: actions["normal"].triggered.connect(lambda: _select_mode("normal"))
        if "b64" in actions: actions["b64"].triggered.connect(lambda: _select_mode("b64"))
        if "script" in actions: actions["script"].triggered.connect(lambda: _select_mode("script"))

        def _copy_current():
            try:
                key = self._copy_mode
                payload = {"normal": normal, "b64": b64, "script": script_text}.get(key, "")
                payload = (payload or "").strip()
                if not payload:
                    QMessageBox.warning(self, "Copiar", "Nada para copiar.")
                    return
                cb = QGuiApplication.clipboard()
                cb.setText(payload, mode=QClipboard.Clipboard)
                try:
                    cb.setText(payload, mode=QClipboard.Selection)
                except Exception:
                    pass
                self.status.setText(f"Copiado ✓ ({'Normal' if key=='normal' else ('Base64' if key=='b64' else 'Script')})")
                self.copy_clicked.emit(payload)
            except Exception as e:
                logger.error(f"[GuideCard] copy: {e}")
                QMessageBox.critical(self, "Erro", f"Não foi possível copiar. {e}")

        btn.clicked.connect(_copy_current)
        return btn

    def _emit_ssh(self, step: dict, cmd_text: str = ""):
        try:
            host = (step.get("host") or "attacker").strip().lower()
            if host == "vitima":
                host = "victim"
            self.status.setText("Abrindo SSH…")
            self.btn_ssh.setEnabled(False)
            self.btn_ssh.setText(f"SSH em {host} (abrindo…)")
            self.ssh_clicked.emit(host, cmd_text or "")
        except Exception as e:
            logger.error(f"[GuideCard] ssh emit: {e}")
            self.status.setText("Falha ao abrir SSH ✖")

    def set_ssh_done(self, msg: str = "Comando enviado via SSH ✓"):
        self.status.setText(msg)
        try:
            self.btn_ssh.setEnabled(True)
            base = self.step.get('host', 'guest')
            self.btn_ssh.setText(f"SSH em {base}")
        except Exception as e:
            logger.error(f"[GuideCard] ssh done: {e}")

    # ---------- Header icon ----------
    def _refresh_header_icon(self):
        try:
            pm: QPixmap = ICONS.get_icon(self._role, self._vis, 22)
            if not pm.isNull():
                self.header_icon.setPixmap(pm)
                self.header_icon.setToolTip(f"{self._role} | {self._vis}")
            else:
                self.header_icon.clear()
        except Exception as e:
            logger.error(f"[GuideCard] header icon: {e}")

    # ---------- API pública ----------
    def set_machine_visibility(self, vis: str):
        try:
            self._vis = "online" if vis == "online" else "offline"
            self._refresh_header_icon()
        except Exception as e:
            logger.error(f"[GuideCard] set_machine_visibility: {e}")

    def set_machine_role(self, role: str):
        try:
            r = role_from_host(role)
            self._role = r
            self._refresh_header_icon()
        except Exception as e:
            logger.error(f"[GuideCard] set_machine_role: {e}")

    def get_role(self) -> str:
        return self._role

    def matches_role(self, role: str) -> bool:
        try:
            return self._role == role_from_host(role)
        except Exception:
            return False

    def matches_host(self, host_or_name: str) -> bool:
        """Match direto por texto do host para cobrir steps que usam nome exato ('attacker', 'sensor1', etc.)."""
        try:
            a = (host_or_name or "").strip().lower()
            b = (self.step.get("host") or "").strip().lower()
            return a == b
        except Exception:
            return False

    def _on_done(self):
        try:
            logger.info(f"[GuideCard] Marcando concluído: {self.step.get('id') or self.step.get('title')}")
            self.set_done(True)
            self.mark_done.emit(self.step)
            self._blink_done_feedback()
        except Exception as e:
            logger.error(f"[GuideCard] _on_done erro: {e}")
            try:
                QMessageBox.warning(self, "Marcar ✓", f"Falha: {e}")
            except Exception:
                pass

    def _blink_done_feedback(self):
        try:
            effect = self.graphicsEffect()
            if not isinstance(effect, QGraphicsOpacityEffect):
                effect = QGraphicsOpacityEffect(self)
                self.setGraphicsEffect(effect)
            anim = QPropertyAnimation(effect, b"opacity", self)
            anim.setDuration(220)
            anim.setStartValue(0.55)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.InOutCubic)
            anim.start(QPropertyAnimation.DeleteWhenStopped)
        except Exception as e:
            logger.error(f"[GuideCard] blink feedback falhou: {e}")
