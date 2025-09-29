# -*- coding: utf-8 -*-
import logging
from PySide6.QtCore import Qt, Signal, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QAction, QFontMetrics, QPixmap, QGuiApplication, QClipboard, QColor
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QMenu, QSizePolicy,
    QPushButton, QPlainTextEdit, QMessageBox, QGraphicsOpacityEffect
)

from app.ui.components.anim_utils import crossfade_label_pixmap, HoverGlowFilter
from app.ui.components.machine_avatar import ICONS
from app.ui.guide.spinner import _MiniSpinner
from app.ui.guide.guide_utils import build_copy_payloads

logger = logging.getLogger("[GuideCard]")
if not hasattr(logger, "warn"):
    logger.warn = logger.warning


def role_from_host(host: str) -> str:
    try:
        h = (host or "").strip().lower()
        if h.startswith("vit"):  # vitima/victim
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

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        header = QHBoxLayout(); header.setContentsMargins(0, 0, 0, 0)

        # ícone pequeno — mesma escala do MachineCard (22px)
        self.header_icon = QLabel()
        self.header_icon.setObjectName("GuideHeaderIcon")
        self.header_icon.setFixedSize(22, 22)

        try:
            self._icon_hover = HoverGlowFilter(
                self.header_icon,
                online_color=QColor(0, 255, 180, 130),
                offline_color=QColor(255, 80, 80, 130),
                radius=16,
                duration=130
            )
        except Exception as e:
            logger.error(f"[GuideCard] hover glow init: {e}")


        self._refresh_header_icon()
        header.addWidget(self.header_icon)

        title = QLabel(f"{self.idx:02d}. {self.step.get('title','Passo')}")
        title.setObjectName("GuideTitle")
        title.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        fm = QFontMetrics(title.font())
        title.setToolTip(title.text())
        title.setText(fm.elidedText(title.text(), Qt.ElideRight, 720))
        header.addWidget(title)

        host_lbl = QLabel(self.step.get("host", "guest"))
        host_lbl.setObjectName("GuideHost")
        header.addWidget(host_lbl)

        header.addStretch(1)
        layout.addLayout(header)

        # Descrição (aceita 'description' ou 'desc')
        desc_text = (self.step.get("description") or self.step.get("desc") or "—")
        desc = QLabel(desc_text)
        desc.setObjectName("GuideDesc")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # Comando (aceita 'command' ou 'cmd')
        cmd_text = (self.step.get("command") or self.step.get("cmd") or "")
        self.cmd_box = QPlainTextEdit(cmd_text)
        self.cmd_box.setObjectName("GuideCmdBox")
        self.cmd_box.setReadOnly(False)
        self.cmd_box.setPlaceholderText("Comando a executar no host deste passo…")
        self.cmd_box.setFixedHeight(86)
        layout.addWidget(self.cmd_box)

        # Artefatos esperados
        art = self.step.get("artifacts", [])
        art_label = QLabel("Artefatos esperados: " + (", ".join(art) if art else "—"))
        art_label.setObjectName("GuideArtifacts")
        art_label.setWordWrap(True)
        layout.addWidget(art_label)

        # Ações
        row = QHBoxLayout()
        self.copy_btn = self._build_copy_button(self.step)
        self.btn_run = QPushButton(f"Rodar no {self.step.get('host', 'guest')}")
        self.btn_ssh = QPushButton(f"SSH em {self.step.get('host','guest')}")
        self.btn_done = QPushButton("Marcar ✓")

        for b in (self.btn_run, self.btn_ssh, self.btn_done):
            b.setObjectName("HoloBtn")

        self.btn_run.clicked.connect(lambda: self.run_clicked.emit(self.step))
        self.btn_ssh.clicked.connect(lambda: self._emit_ssh(self.step, self.cmd_box.toPlainText()))
        self.btn_done.clicked.connect(self._on_done)

        row.addWidget(self.copy_btn)
        row.addWidget(self.btn_run)
        row.addWidget(self.btn_ssh)
        row.addWidget(self.btn_done)
        row.addStretch(1)
        layout.addLayout(row)

        # Status visual do passo (necessário para timeline/progresso)
        self.status = QLabel("A fazer")
        self.status.setObjectName("GuideStatus")
        layout.addWidget(self.status)

        # Estado inicial + habilitação de botões dependentes de VM online
        self._set_status_state("idle")
        try:
            self._update_action_enabled()
        except Exception as e:
            logger.error(f"[GuideCard] _update_action_enabled init: {e}")

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
        if tags: parts.append(f"Tags: {', '.join(tags)}")
        return " | ".join(parts)

    def _build_copy_button(self, step: dict) -> QPushButton:
        btn = QPushButton("Copiar")
        btn.setObjectName("HoloBtn")
        btn.setMenu(self._build_copy_menu(step))

        def _copy_current():
            try:
                normal, b64, script_text = build_copy_payloads(step)
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

    def _build_copy_menu(self, step: dict) -> QMenu:
        menu = QMenu(self)
        a_norm = QAction("Normal", self)
        a_b64 = QAction("Base64", self)
        a_script = QAction("Script .sh", self)
        for a in (a_norm, a_b64, a_script):
            menu.addAction(a)
        a_norm.triggered.connect(lambda: setattr(self, "_copy_mode", "normal"))
        a_b64.triggered.connect(lambda: setattr(self, "_copy_mode", "b64"))
        a_script.triggered.connect(lambda: setattr(self, "_copy_mode", "script"))
        return menu

    def _emit_ssh(self, step: dict, cmd_text: str = ""):
        try:
            host = (step.get("host") or "attacker").strip().lower()
            if host == "vitima":
                host = "victim"
            self.status.setText("Abrindo SSH…")
            self.btn_ssh.setEnabled(False)
            self.ssh_clicked.emit(host, cmd_text)
        except Exception as e:
            logger.error(f"[GuideCard] ssh: {e}")
            QMessageBox.critical(self, "SSH", f"Falha ao abrir SSH: {e}")
        finally:
            try:
                self.btn_ssh.setEnabled(self._vis == "online")
            except Exception:
                pass

    def _on_done(self):
        try:
            self.status.setText("Concluído ✓")
            self._set_status_state("done")
            self._blink_done_feedback()
            self.mark_done.emit(self.step)
        except Exception as e:
            logger.error(f"[GuideCard] done: {e}")

    def _refresh_header_icon(self):
        try:
            pm: QPixmap | None = ICONS.get_icon(self._role, self._vis, 22)
            if pm is not None and not pm.isNull():
                self.header_icon.setProperty("vis", self._vis)
                crossfade_label_pixmap(self.header_icon, pm, duration=170)
                self.header_icon.setToolTip(f"{self._role} | {self._vis}")
            else:
                self.header_icon.clear()
        except Exception as e:
            logger.error(f"[GuideCard] header icon: {e}")

    def _update_action_enabled(self):
        try:
            online = (self._vis == "online")
            if hasattr(self, "btn_run") and self.btn_run:
                self.btn_run.setEnabled(online)
                tip = "Disponível quando a máquina estiver ONLINE." if not online else "Executar no host."
                self.btn_run.setToolTip(tip)
            if hasattr(self, "btn_ssh") and self.btn_ssh:
                self.btn_ssh.setEnabled(online)
                tip = "Disponível quando a máquina estiver ONLINE." if not online else "Abrir sessão SSH."
                self.btn_ssh.setToolTip(tip)
        except Exception as e:
            logger.error(f"[GuideCard] _update_action_enabled: {e}")

    # ---------- API pública ----------
    def set_machine_visibility(self, vis: str):
        try:
            self._vis = "online" if vis == "online" else "offline"
            self._refresh_header_icon()
            self._update_action_enabled()
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

    def matches_host(self, name: str) -> bool:
        try:
            host = (self.step.get("host") or "").strip().lower()
            return host == (name or "").strip().lower()
        except Exception:
            return False

    # ---------- Estados visuais ----------
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
            final_txt = "Concluído ✓" if ok else "Finalizado"
            if self._spin:
                try:
                    self._spin.stop(final_txt)
                except Exception:
                    self.status.setText(final_txt)
                self._spin = None
            else:
                self.status.setText(final_txt)

            self._set_status_state("done")
            self.set_machine_visibility("offline")
        except Exception as e:
            logger.error(f"[GuideCard] set_done: {e}")

    def set_cancelled(self):
        try:
            final_txt = "Cancelado ⏸"
            if self._spin:
                try:
                    self._spin.stop(final_txt)
                except Exception:
                    self.status.setText(final_txt)
                self._spin = None
            else:
                self.status.setText(final_txt)
            self._set_status_state("cancelled")
            self.set_machine_visibility("offline")
        except Exception as e:
            logger.error(f"[GuideCard] set_cancelled: {e}")

    def set_error(self):
        try:
            final_txt = "Falhou ✖"
            if self._spin:
                try:
                    self._spin.stop(final_txt)
                except Exception:
                    self.status.setText(final_txt)
                self._spin = None
            else:
                self.status.setText(final_txt)

            self._set_status_state("error")
            self.set_machine_visibility("offline")
        except Exception as e:
            logger.error(f"[GuideCard] set_error: {e}")

    # ---------- Copy/SSH ----------
    def set_ssh_done(self, msg: str = "Comando enviado via SSH ✓"):
        self.status.setText(msg)
        try:
            self.btn_ssh.setEnabled(True)
            base = self.step.get('host', 'guest')
            self.btn_ssh.setText(f"SSH em {base}")
        except Exception as e:
            logger.error(f"[GuideCard] ssh done: {e}")

    # ---------- Feedback ----------
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
