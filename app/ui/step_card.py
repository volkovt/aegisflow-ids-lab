import base64
import logging, time, json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, List
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QWidget, QFrame, QMessageBox, QPlainTextEdit, QFileDialog, QToolButton, QMenu, QApplication
)
from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, Signal, QThread
from PySide6.QtGui import QGuiApplication, QClipboard, QAction, QActionGroup, QCursor

# Parser “oficial”
from app.core.yaml_parser import parse_yaml_to_steps

# -----------------------------
# Logger
# -----------------------------
logger = logging.getLogger("[Guide]")

def _safe(obj):
    try:
        return repr(obj)
    except Exception:
        return f"<{type(obj).__name__}>"

def _here(tag: str):
    # marcador rápido para filtrar no log
    logger.warning(f"[Guide] >>> {tag}")

logger.warning("[Guide] step_card.py importado")

# -----------------------------
# Helpers
# -----------------------------
def _wrap_b64_for_copy(cmd: str) -> str:
    """
    Gera um transporte Base64 seguro a partir de 'cmd'.
    Heurísticas:
      - Se já houver base64 -d, retorna cmd (já está blindado).
      - Se detectar 'nohup' e '&', preserva background.
      - Se detectar 'sudo', usa 'sudo bash -se' no destino; senão 'bash -se'.
      - Se detectar padrão 'bash -lc' com bloco entre aspas, tenta extrair o script interno.
      - Caso contrário, encapsula 'cmd' como script para o bash interpretar.
    """
    try:
        text = cmd.strip()
        if "| base64 -d |" in text:
            return text  # já está blindado

        has_nohup = "nohup " in text
        ends_bg = text.rstrip().endswith("&")
        wants_sudo = text.startswith("sudo ") or " sudo " in text

        # 1) tenta extrair script interno de 'bash -lc' ou 'bash -c'
        inner_script = None
        m = re.search(r"""bash\s+-l?c\s+(['"])(?P<body>.*)\1\s*$""", text, re.DOTALL)
        if m:
            inner_script = m.group("body")

        # 2) determina o runner de destino
        runner = "bash -se"
        if wants_sudo:
            runner = "sudo " + runner

        # 3) escolhe o payload a codificar
        if inner_script:
            payload = inner_script
        else:
            payload = text

        b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        core = f"echo {b64} | base64 -d | {runner}"

        if has_nohup or ends_bg:
            # Encapsula para fundo; evita escapar demais
            return f'nohup sh -c {shlex.quote(core)} >/dev/null 2>&1 &'

        return core
    except Exception as e:
        logging.getLogger("VagrantLabUI").error(f"[Guide] falha ao montar b64: {e}")
        # fallback mínimo: retorna cmd original para não travar UX
        return cmd

def _is_heredoc(cmd: str) -> bool:
    t = cmd or ""
    return ("<<'__EOF__'" in t) or ('<<"__EOF__"' in t) or ('<<__EOF__' in t) or ("\n__EOF__" in t)


# -----------------------------
# Mini spinner
# -----------------------------
class _MiniSpinner:
    FRAMES = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]
    def __init__(self, label: QLabel, base="Executando"):
        self.label = label
        self.base = base
        self.i = 0
        self.timer = QTimer(label)
        self.timer.timeout.connect(self.tick)
        logger.warning(f"[Guide][Spinner] criado base={base} label={_safe(label)}")

    def start(self, text=None):
        if text:
            self.base = text
        self.timer.start(90)
        logger.warning(f"[Guide][Spinner] start base={self.base}")

    def stop(self, text=None):
        self.timer.stop()
        if text is not None:
            self.label.setText(text)
        logger.warning(f"[Guide][Spinner] stop set='{text}'")

    def tick(self):
        try:
            f = _MiniSpinner.FRAMES[self.i % len(_MiniSpinner.FRAMES)]
            self.i += 1
            self.label.setText(f"{self.base} {f}")
        except Exception as e:
            logger.warning(f"[Guide][Spinner] tick erro: {e}")

# -----------------------------
# Workers
# -----------------------------
class _FnWorker(QThread):
    result = Signal(object)
    error = Signal(str)
    _seq = 0
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        _FnWorker._seq += 1
        self.id = f"FnWorker#{_FnWorker._seq}"
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        logger.warning(f"[Guide][{self.id}] criado fn={getattr(fn,'__name__',fn)} args={len(args)} kwargs={list(kwargs.keys())}")

    def run(self):
        try:
            _here(f"{self.id}.run:start")
            r = self.fn(*self.args, **self.kwargs)
            _here(f"{self.id}.run:emit result")
            self.result.emit(r)
        except Exception as e:
            logger.error(f"[Guide][{self.id}] falhou: {e}", exc_info=True)
            self.error.emit(str(e))
        finally:
            _here(f"{self.id}.run:end")

class _StreamWorker(QThread):
    line = Signal(str)
    finished_ok = Signal()
    error = Signal(str)

    _seq = 0

    def __init__(self, ssh, host, cmd, timeout_s=120):
        super().__init__()
        _StreamWorker._seq += 1
        self.id = f"StreamWorker#{_StreamWorker._seq}"

        self.ssh = ssh
        self.host = host
        self.cmd = cmd
        self.timeout_s = timeout_s
        self._stop_flag = False
        self._last_rc = None  # armazena o exit code observado

    def _emit_chunk(self, text: str):
        if not text:
            return
        self.line.emit(text)
        # Captura do sentinela [guide] __RC=<n>
        m = re.search(r"\[guide\]\s*__RC=(\d+)", text)
        if m:
            try:
                self._last_rc = int(m.group(1))
            except Exception:
                self._last_rc = 1

    def stop(self):
        self._stop_flag = True
        logger.warning(f"[Guide][{self.id}] stop solicitado")

    def run(self):
        try:
            # Encapa para sempre imprimir o exit code no final do stream
            wrapped = "{ " + self.cmd + " ; } ; rc=$?; printf \"\\n[guide] __RC=%s\\n\" \"$rc\"; exit $rc"

            if hasattr(self.ssh, "run_command_stream"):
                for chunk in self.ssh.run_command_stream(self.host, wrapped, timeout_s=self.timeout_s):
                    if self._stop_flag:
                        break
                    self._emit_chunk(chunk)
            else:
                self._run_no_stream()

        except Exception as e:
            self.error.emit(str(e))
            return

        if self._stop_flag:
            self.error.emit("Cancelado")
            return

        rc = 0 if self._last_rc is None else self._last_rc
        if rc != 0:
            self.error.emit(f"Comando falhou (rc={rc})")
            return

        self.finished_ok.emit()

    def _run_no_stream(self, use_classic: bool = False):
        try:
            logger.warning(f"[Guide][{self.id}] _run_no_stream classic={use_classic}")

            if _is_heredoc(self.cmd):
                logger.warning(f"[Guide][{self.id}] detected heredoc; sending raw to Paramiko")
                out = self.ssh.run_command(self.host, self.cmd, timeout=self.timeout_s)
            else:
                out = (
                    self.ssh.run_command(self.host, f"bash -lc '{self.cmd}'", timeout=self.timeout_s)
                    if use_classic else
                    self.ssh.run_command_cancellable(self.host, self.cmd, timeout_s=self.timeout_s)
                )

            self._emit_block_output(out)
        except Exception as e:
            self.error.emit(str(e))
            return

    def _emit_block_output(self, out: Any):
        try:
            logger.warning(f"[Guide][{self.id}] _emit_block_output type={type(out).__name__}")
            if out is None:
                return
            if isinstance(out, str):
                for line in out.splitlines():
                    self.line.emit(line)
                return
            if isinstance(out, tuple) and len(out) >= 2:
                stdout, stderr = out[0] or "", out[1] or ""
                for line in str(stdout).splitlines():
                    self.line.emit(f"[stdout] {line}")
                for line in str(stderr).splitlines():
                    self.line.emit(f"[stderr] {line}")
                return
            if isinstance(out, dict):
                stdout, stderr = out.get("stdout", ""), out.get("stderr", "")
                for line in str(stdout).splitlines():
                    self.line.emit(f"[stdout] {line}")
                for line in str(stderr).splitlines():
                    self.line.emit(f"[stderr] {line}")
                return
            self.line.emit(str(out))
        except Exception as e:
            logger.warning(f"[Guide][{self.id}] _emit_block_output erro: {e}")

# -----------------------------
# Card de passo
# -----------------------------
class StepCard(QFrame):
    run_clicked = Signal(dict)
    copy_clicked = Signal(str)
    mark_done = Signal(dict)
    ssh_clicked = Signal(str, str)
    _seq = 0

    def __init__(self, idx: int, step: dict):
        super().__init__()
        StepCard._seq += 1
        self.cid = f"Card#{StepCard._seq}"
        self.setObjectName("GuideCard")
        self.step = step
        self.idx = idx
        logger.warning(f"[Guide][{self.cid}] criando: idx={idx} id_step={step.get('id')}")
        self._build()

    def _build(self):
        _here(f"{self.cid}._build:start")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        title = QLabel(f"{self.idx:02d}. {self.step.get('title','Passo')}")
        title.setObjectName("GuideTitle")

        desc = QLabel(self.step.get("description",""))
        desc.setObjectName("GuideDesc")
        desc.setWordWrap(True)

        meta = QLabel(self._meta_text())
        meta.setObjectName("GuideMeta")
        meta.setWordWrap(True)

        cmd_to_show = self.step.get("command_normal") or self.step.get("command_b64") or self.step.get("command") or ""
        cmd_box = QPlainTextEdit(cmd_to_show)
        cmd_box.setReadOnly(True)
        cmd_box.setFixedHeight(64)
        cmd_box.setObjectName("GuideCmd")
        try:
            shown = "command_normal" if self.step.get("command_normal") else ("command_b64" if self.step.get("command_b64") else (
                "command" if self.step.get("command") else "—"))
            cmd_box.setToolTip(f"Mostrando: {shown}")
        except Exception:
            pass

        art = self.step.get("artifacts", [])
        art_label = QLabel("Artefatos esperados: " + (", ".join(art) if art else "—"))
        art_label.setObjectName("GuideArtifacts")
        art_label.setWordWrap(True)

        row = QHBoxLayout()
        copy_btn = self._build_copy_button(self.step)
        btn_run  = QPushButton(f"Rodar no {self.step.get('host','guest')}")
        host_label = self.step.get('host', 'guest')
        btn_ssh = QPushButton(f"SSH em {host_label}")
        self.btn_ssh = btn_ssh
        btn_done = QPushButton("Marcar ✓")
        self.status = QLabel("A fazer")
        self.status.setObjectName("GuideStatus")

        btn_run.clicked.connect(lambda: self._emit_run(self.step))
        btn_ssh.clicked.connect(lambda: self._emit_ssh(self.step, cmd_box.toPlainText()))
        btn_done.clicked.connect(lambda: self._on_done())

        row.addWidget(copy_btn)
        row.addWidget(btn_run)
        row.addWidget(btn_ssh)
        row.addWidget(btn_done)
        row.addStretch(1)
        row.addWidget(self.status)

        layout.addWidget(title)
        layout.addWidget(desc)
        layout.addWidget(meta)
        layout.addWidget(cmd_box)
        layout.addWidget(art_label)
        layout.addLayout(row)
        _here(f"{self.cid}._build:end")

    def _set_status_state(self, state: str):
        import logging
        logger = logging.getLogger("VagrantLabUI")
        try:
            self.status.setProperty("state", state)
            self.status.style().unpolish(self.status)
            self.status.style().polish(self.status)
            self.status.update()
            logger.info(f"[Guide][{self.cid}] status_state={state}")
        except Exception as e:
            logger.warning(f"[Guide][{self.cid}] status_state falhou: {e}")

    def _build_copy_button(self, step: dict) -> QToolButton:
        logger = logging.getLogger("VagrantLabUI")

        btn = QToolButton(self)
        btn.setToolTip("Selecione o modo no menu e clique para copiar")
        btn.setPopupMode(QToolButton.MenuButtonPopup)

        menu = QMenu(btn)

        # Fontes
        normal = step.get("command_normal") or step.get("command") or ""
        legacy = step.get("command") or ""
        try:
            b64 = step.get("command_b64") or (_wrap_b64_for_copy(normal or legacy) if (normal or legacy) else "")
        except Exception as e:
            logger.error(f"[Guide] Falha ao montar b64: {e}")
            b64 = step.get("command_b64") or ""
        script_text = step.get("script", "")

        # Modelagem dos modos
        modes = [("normal", "Normal", normal or legacy), ("b64", "Base64", b64)]
        if script_text:
            modes.append(("script", "Script puro", script_text))

        # Grupo checkable para refletir modo escolhido
        group = QActionGroup(btn)
        group.setExclusive(True)
        actions = {}
        label_by_key = {k: lbl for k, lbl, _ in modes}

        for key, label, _ in modes:
            act = QAction(f"Usar {label}", btn)
            act.setCheckable(True)
            group.addAction(act)
            menu.addAction(act)
            actions[key] = act

        # Estado inicial do modo
        self._copy_mode = getattr(self, "_copy_mode", None) or ("normal" if "normal" in actions else modes[0][0])
        if self._copy_mode not in actions:
            self._copy_mode = modes[0][0]
        actions[self._copy_mode].setChecked(True)

        # Texto do botão reflete o modo atual
        btn.setText(f"Copiar ({label_by_key[self._copy_mode]})")
        btn.setMenu(menu)

        def _update_caption():
            try:
                btn.setText(f"Copiar ({label_by_key[self._copy_mode]})")
                # Só atualiza status se já existir (durante build ainda não existe)
                if hasattr(self, "status"):
                    self.status.setText(f"Modo de cópia: {label_by_key[self._copy_mode]}")
                logger.info(f"[Guide] Modo de cópia selecionado: {self._copy_mode}")
            except Exception as e:
                logger.warning(f"[Guide] update_caption erro: {e}")

        def _select_mode(key: str):
            try:
                self._copy_mode = key
                for k, act in actions.items():
                    act.setChecked(k == key)
                _update_caption()
            except Exception as e:
                logger.error(f"[Guide] select_mode falhou: {e}")

        # Conecta cada item do menu para apenas SELECIONAR o modo (não copia aqui)
        if "normal" in actions: actions["normal"].triggered.connect(lambda: _select_mode("normal"))
        if "b64" in actions:    actions["b64"].triggered.connect(lambda: _select_mode("b64"))
        if "script" in actions: actions["script"].triggered.connect(lambda: _select_mode("script"))

        # Clique no botão copia conforme o modo atual
        def _copy_current_mode():
            try:
                key = self._copy_mode
                payload = ""
                if key == "normal":
                    payload = (normal or legacy).strip()
                elif key == "b64":
                    payload = (b64 or "").strip()
                elif key == "script":
                    payload = (script_text or "").strip()

                if not payload:
                    QMessageBox.warning(self, "Copiar", "Nada para copiar.")
                    return

                self._on_copy(payload)  # já seta "Copiado ✓"
                # reforça o feedback com o modo
                if hasattr(self, "status"):
                    self.status.setText(f"Copiado ✓ ({label_by_key[key]})")
                logger.info(f"[Guide] Copiado para a área de transferência ({key}, {len(payload)} chars).")
            except Exception as e:
                logger.error(f"[Guide] Falha ao copiar (modo={getattr(self, '_copy_mode', '?')}): {e}")
                QMessageBox.critical(self, "Erro", f"Não foi possível copiar. {e}")

        btn.clicked.connect(_copy_current_mode)
        return btn

    def _emit_ssh(self, step: dict, cmd_text: str = ""):
        try:
            host = (step.get("host") or "attacker").strip().lower()
            if host == "vitima":
                host = "victim"
            self.status.setText("Abrindo SSH…")
            try:
                if hasattr(self, "btn_ssh"):
                    self.btn_ssh.setEnabled(False)
                    self.btn_ssh.setText(f"SSH em {host} (abrindo…)")
            except Exception:
                pass
            self.ssh_clicked.emit(host, cmd_text or "")
        except Exception as e:
            logger.error(f"[Guide][{self.cid}] Falha ao acionar SSH: {e}")
            self.status.setText("Falha ao abrir SSH ✖")

    def set_ssh_done(self, msg: str = "Comando enviado via SSH ✓"):
        self.status.setText(msg)
        try:
            if hasattr(self, "btn_ssh"):
                self.btn_ssh.setEnabled(True)
                base = self.step.get('host', 'guest')
                self.btn_ssh.setText(f"SSH em {base}")
        except Exception as e:
            logger.warning(f"[Guide][{self.cid}] reabilitar SSH falhou: {e}")

    def _emit_run(self, step: dict):
        logger.warning(f"[Guide][{self.cid}] RUN clicado step_id={step.get('id')} host={step.get('host')}")
        self.run_clicked.emit(step)

    def _on_copy(self, text: str):
        try:
            payload = (text or "").strip()
            cb = QGuiApplication.clipboard()
            cb.setText(payload, mode=QClipboard.Clipboard)
            try:
                cb.setText(payload, mode=QClipboard.Selection)
            except Exception:
                pass
            self.status.setText("Copiado ✓")
            logger.info(f"[Guide] Comando copiado ({len(payload)} chars).")
        except Exception as e:
            self.status.setText("Falha ao copiar ✖")
            logger.error(f"[Guide] Falha ao copiar comando: {e}")

    def _on_done(self):
        logger = logging.getLogger("VagrantLabUI")
        logger.warning(f"[Guide][{self.cid}] DONE marcado")
        try:
            self.status.setText("Concluído ✓")
            self._set_status_state("done")
        except Exception as e:
            logger.error(f"[Guide][{self.cid}] erro ao marcar done: {e}")
        self.mark_done.emit(self.step)

    def set_running(self):
        logger = logging.getLogger("VagrantLabUI")
        logger.warning(f"[Guide][{self.cid}] set_running()")
        try:
            self.status.setText("Executando…")
            self._set_status_state("running")
            self._spin = getattr(self, "_spin", None)
            if not self._spin:
                self._spin = _MiniSpinner(self.status, "Executando")
            self._spin.start()
        except Exception as e:
            logger.error(f"[Guide][{self.cid}] set_running erro: {e}")

    def set_idle(self):
        logger = logging.getLogger("VagrantLabUI")
        logger.warning(f"[Guide][{self.cid}] set_idle()")
        try:
            if hasattr(self, "_spin") and self._spin:
                try:
                    self._spin.stop("A fazer")
                except Exception:
                    self.status.setText("A fazer")
            else:
                self.status.setText("A fazer")
            self._set_status_state("idle")
        except Exception as e:
            logger.error(f"[Guide][{self.cid}] set_idle erro: {e}")

    def _meta_text(self):
        tags = self.step.get("tags", [])
        eta  = self.step.get("eta", "")
        host = self.step.get("host","guest")
        parts = []
        if eta: parts.append(f"ETA: {eta}")
        parts.append(f"Host: {host}")
        if tags: parts.append("Tags: " + ", ".join(tags))
        return " • ".join(parts)

# -----------------------------
# Diálogo do Guia com console
# -----------------------------
class ExperimentGuideDialog(QDialog):
    def __init__(self, yaml_path: str, ssh, vagrant, lab_dir: str, project_root: str, parent=None):
        super().__init__(parent)
        self._load_theme()
        try:
            self._apply_window_flags()
        except Exception as e:
            logger.warning(f"[Guide] _apply_window_flags erro: {e}")

        _here("Dialog.__init__:start")

        self.setObjectName("GuideDialog")
        try:
            self.yaml_path = str(Path(yaml_path).resolve()) if yaml_path else ""
            if self.yaml_path and Path(self.yaml_path).is_dir():
                logger.warning("[Guide] yaml_path aponta para diretório; forçando modo oficial.")
                self.yaml_path = ""
        except Exception as e:
            logger.warning(f"[Guide] resolve do yaml_path falhou: {e}")
            self.yaml_path = yaml_path or ""

        self._official_mode = (not self.yaml_path) or (not Path(self.yaml_path).exists())
        self._only_yaml_actions = bool(self.yaml_path) and (not self._official_mode)

        self.ssh = ssh
        self.vagrant = vagrant
        self.lab_dir = Path(lab_dir)
        self.project_root = Path(project_root)

        self.timeline = {}
        self.cards: List[StepCard] = []
        self._workers: set[QThread] = set()
        self._stream_worker: _StreamWorker | None = None
        self._loader_worker: _FnWorker | None = None
        self._watchdog: QTimer | None = None
        self._batch_running = False
        self._batch_queue = []

        self._rendered_fallback = False
        self._rendered_real = False
        self._ignore_loader_results = False

        logger.warning(f"[Guide][Dialog] yaml={self.yaml_path} lab_dir={self.lab_dir} project_root={self.project_root}")
        logger.warning(f"[Guide][Dialog] parent={_safe(parent)} ssh={_safe(type(self.ssh))} vagrant={_safe(type(self.vagrant))}")

        self._build_ui()
        self._update_yaml_header_label()

        # Garantir que a janela aparece e só então começamos tarefas
        QTimer.singleShot(0, self._start_loading_with_watchdog)
        _here("Dialog.__init__:end")

    def _apply_window_flags(self):
        flags = (
                Qt.Window
                | Qt.WindowTitleHint
                | Qt.WindowSystemMenuHint
                | Qt.WindowMinimizeButtonHint
                | Qt.WindowMaximizeButtonHint
                | Qt.WindowCloseButtonHint
        )
        self.setWindowFlags(flags)

        try:
            self.setSizeGripEnabled(True)
        except Exception:
            pass

        try:
            screen = QApplication.screenAt(QCursor.pos())
            if screen:
                size = 600
                sg = screen.geometry()
                x = sg.x() + (sg.width() - size) // 2
                y = sg.y() + (sg.height() - size) // 2
                self.setGeometry(x, y, size, size)
            else:
                self.setGeometry(100, 100, 600, 600)
        except Exception as e:
            print(f"Falha ao posicionar janela: {e}")
            self.setGeometry(100, 100, 600, 600)

        logger.warning("[Guide] window flags aplicados: min/max/close habilitados")

    def showEvent(self, e):
        super().showEvent(e)
        try:
            try:
                self.setWindowOpacity(1.0)
            except Exception:
                pass
            QTimer.singleShot(0, self._bring_to_front)
            logger.warning(
                f"[Guide][Dialog] showEvent done | visible={self.isVisible()} | opacity={getattr(self, 'windowOpacity', lambda: 1.0)()}")
        except Exception as ex:
            logger.warning(f"[Guide][Dialog] showEvent erro: {ex}")

    def _bring_to_front(self):
        try:
            parent = self.parent()
            if parent and parent.isVisible():
                pg = parent.geometry()
                my = self.frameGeometry()
                my.moveCenter(pg.center())
                self.move(my.topLeft())
            else:
                from PySide6.QtGui import QGuiApplication
                scr = QGuiApplication.primaryScreen()
                if scr:
                    sg = scr.availableGeometry()
                    my = self.frameGeometry()
                    my.moveCenter(sg.center())
                    self.move(my.topLeft())

            try:
                self.setWindowOpacity(1.0)
            except Exception:
                pass
            self.show()
            self.raise_()
            self.activateWindow()
            logger.warning("[Guide][Dialog] bring_to_front aplicado")
        except Exception as e:
            logger.warning(f"[Guide][Dialog] bring_to_front erro: {e}")

    def _build_ui(self):
        _here("Dialog._build_ui:start")
        self.setWindowTitle("Guia do Experimento")
        self.setMinimumSize(980, 720)
        main = QVBoxLayout(self)
        main.setContentsMargins(14, 14, 14, 10)
        main.setSpacing(10)

        header = QHBoxLayout()
        self.lbl_title = QLabel("Guia do Experimento")
        self.lbl_title.setObjectName("GuideHeader")
        self.lbl_yaml = QLabel("oficial (sem YAML)" if self._official_mode else Path(self.yaml_path).name)
        self.lbl_yaml.setObjectName("GuideYaml")
        self.lbl_yaml.setToolTip(
            "Modo oficial (parser) sem YAML selecionado — escolha um YAML para ver ações específicas."
            if self._official_mode else self.yaml_path
        )
        self.btn_pick_yaml = QPushButton("Escolher YAML…", self)
        self.btn_pick_yaml.setToolTip("Carregar um arquivo .yaml diretamente no guia e renderizar os passos.")
        self.btn_pick_yaml.clicked.connect(self._on_pick_yaml_in_guide)

        self.btn_reload = QPushButton("Recarregar (oficial)")
        self.btn_reload.clicked.connect(self._reload_official)

        self.btn_clear_tests = QPushButton("Limpar testes")
        self.btn_run_all = QPushButton("Rodar todos")
        self.btn_mark_all_done = QPushButton("Marcar todos ✓")

        self.btn_clear_tests.setToolTip("Remove os cards atuais e zera a timeline do YAML.")
        self.btn_run_all.setToolTip("Executa todos os passos, em sequência.")
        self.btn_mark_all_done.setToolTip("Marca todos os passos como concluídos (apenas visual e timeline).")

        self.btn_clear_tests.clicked.connect(self._clear_tests)
        self.btn_run_all.clicked.connect(self._run_all_steps)
        self.btn_mark_all_done.clicked.connect(self._mark_all_done)

        self.btn_show_basic = QPushButton("Guia básico agora")
        self.btn_show_basic.setVisible(False)
        self.btn_show_basic.clicked.connect(self._show_basic_fallback)
        header.addWidget(self.lbl_title)
        header.addStretch(1)
        header.addWidget(self.lbl_yaml)
        header.addWidget(self.btn_show_basic)
        header.addWidget(self.btn_pick_yaml)
        header.addWidget(self.btn_reload)
        header.addWidget(self.btn_clear_tests)
        header.addWidget(self.btn_run_all)
        header.addWidget(self.btn_mark_all_done)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.cards_container = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setContentsMargins(6, 6, 6, 6)
        self.cards_layout.setSpacing(10)
        scroll.setWidget(self.cards_container)

        self._loading_label = QLabel("Carregando passos…")
        self._loading_spinner = _MiniSpinner(self._loading_label, "Carregando")
        self._loading_spinner.start()
        self.cards_layout.addWidget(self._loading_label)

        console_bar = QHBoxLayout()
        self.btn_console_clear = QPushButton("Limpar console")
        self.btn_console_save = QPushButton("Salvar log…")
        self.btn_isolate = QPushButton("Isolar atacante (egress guard)")
        self.btn_cancel = QPushButton("Cancelar comandos")
        self.btn_run_runner = QPushButton("Gerar dataset (Runner)")

        console_bar.addWidget(self.btn_console_clear)
        console_bar.addWidget(self.btn_console_save)
        console_bar.addStretch(1)
        console_bar.addWidget(self.btn_isolate)
        console_bar.addWidget(self.btn_cancel)
        console_bar.addWidget(self.btn_run_runner)

        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMinimumHeight(200)
        self.console.setObjectName("GuideConsole")

        self.lbl_footer = QLabel("Pronto para iniciar.")
        self.lbl_footer.setObjectName("GuideFooter")

        self.btn_console_clear.clicked.connect(self.console.clear)
        self.btn_console_save.clicked.connect(self._save_console_to_file)
        self.btn_isolate.clicked.connect(self._toggle_isolation_async)
        self.btn_cancel.clicked.connect(self._cancel_running)
        self.btn_run_runner.clicked.connect(self._run_runner_async)

        main.addLayout(header)
        main.addWidget(scroll, stretch=1)
        main.addLayout(console_bar)
        main.addWidget(self.console, stretch=0)
        main.addWidget(self.lbl_footer)

        # fade-in
        try:
            self.setWindowOpacity(0.0)
            anim = QPropertyAnimation(self, b"windowOpacity")
            anim.setDuration(250)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.InOutQuad)
            anim.start(QPropertyAnimation.DeleteWhenStopped)
        except Exception as e:
            logger.warning(f"[Guide] animação falhou: {e}")

        _here("Dialog._build_ui:end")

    def _timeline_path(self):
        try:
            if not getattr(self, "yaml_path", ""):
                return None
            meta_dir = self.project_root / ".meta"
            meta_dir.mkdir(parents=True, exist_ok=True)
            return meta_dir / (Path(self.yaml_path).stem + "_timeline.json")
        except Exception as e:
            logger.warning(f"[Guide] _timeline_path erro: {e}")
            return None

    def _clear_tests(self):
        try:
            logger.info("[Guide] Limpar testes solicitado")
            try:
                self._cancel_running(wait_worker=True)
            except Exception as e:
                logger.warning(f"[Guide] limpar: cancelamento falhou: {e}")

            removed = 0
            while self.cards_layout.count():
                item = self.cards_layout.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
                    removed += 1
            self.cards.clear()

            self.timeline = {}
            try:
                tpath = self._timeline_path()
                if tpath and tpath.exists():
                    tpath.unlink(missing_ok=True)
                    logger.info(f"[Guide] timeline removida: {tpath}")
            except Exception as e:
                logger.warning(f"[Guide] limpar: falha ao remover timeline: {e}")

            self.console.appendPlainText("[guide] Testes limpos. Timeline zerada.")
            self.lbl_footer.setText("Testes limpos. Carregue um novo YAML ou recarregue o oficial.")
            QMessageBox.information(self, "Limpar testes", "Testes limpos com sucesso.")
        except Exception as e:
            logger.error(f"[Guide] _clear_tests falhou: {e}")
            QMessageBox.critical(self, "Limpar testes", f"Falha: {e}")

    def _run_all_steps(self):
        try:
            if self._stream_worker and self._stream_worker.isRunning():
                self._append_console("[warn] Um passo está em execução. Cancelando antes do lote…")
                self._cancel_running(wait_worker=True)

            if not self.cards:
                QMessageBox.warning(self, "Rodar todos", "Não há passos para executar.")
                return

            self._batch_queue = []
            for c in self.cards:
                try:
                    st = dict(c.step)
                    cmd = (st.get("command") or "").strip()
                    if not cmd:
                        cmd = (st.get("command_normal") or st.get("command_b64") or "").strip()
                        if cmd:
                            st["command"] = cmd
                    if st.get("command"):
                        self._batch_queue.append(st)
                except Exception as e:
                    logger.warning(f"[Guide] montar fila: card inválido: {e}")

            if not self._batch_queue:
                QMessageBox.information(self, "Rodar todos", "Nenhum passo executável encontrado.")
                return

            self._batch_running = True
            self.console.appendPlainText(f"[guide] Rodando {len(self._batch_queue)} passo(s) em sequência…")
            self.lbl_footer.setText("Execução em lote iniciada…")

            next_step = self._batch_queue.pop(0)
            self._run_step_async(next_step)
        except Exception as e:
            logger.error(f"[Guide] _run_all_steps falhou: {e}")
            QMessageBox.critical(self, "Rodar todos", f"Falha: {e}")

    def _mark_all_done(self):
        try:
            if not self.cards:
                QMessageBox.information(self, "Marcar todos ✓", "Não há passos na tela.")
                return

            count = 0
            for card in self.cards:
                try:
                    card._on_done()
                    count += 1
                except Exception as e:
                    logger.warning(f"[Guide] marcar done falhou em um card: {e}")

            self._write_timeline()
            self.lbl_footer.setText(f"Todos marcados como concluídos (total={count}).")
            self.console.appendPlainText(f"[guide] Marcados como concluídos: {count} passo(s).")
            QMessageBox.information(self, "Marcar todos ✓", f"Concluído: {count} passo(s).")
        except Exception as e:
            logger.error(f"[Guide] _mark_all_done falhou: {e}")
            QMessageBox.critical(self, "Marcar todos ✓", f"Falha: {e}")

    def _update_yaml_header_label(self):
        try:
            if getattr(self, "yaml_path", None) and Path(self.yaml_path).exists():
                p = Path(self.yaml_path)
                self.lbl_yaml.setText(f"YAML: {p.name}")
                self.lbl_yaml.setToolTip(str(p))
            else:
                self.lbl_yaml.setText("YAML: (oficial)")
                self.lbl_yaml.setToolTip("Parser oficial (sem arquivo .yaml selecionado).")
        except Exception as e:
            logger.warning(f"[GuideUI] Falha ao atualizar label do YAML: {e}")

    def _on_pick_yaml_in_guide(self):
        try:
            try:
                if getattr(self, "yaml_path", None) and Path(self.yaml_path).exists():
                    start_dir = str(Path(self.yaml_path).parent)
                else:
                    pr = Path(getattr(self, "project_root", "."))
                    start_dir = str(pr / "lab" / "experiments")
            except Exception:
                start_dir = "."

            path, _ = QFileDialog.getOpenFileName(
                self,
                "Escolher YAML de experimento (Guia)",
                start_dir,
                "YAML (*.yaml *.yml)"
            )
            if not path:
                logger.info("[GuideUI] Escolha de YAML cancelada pelo usuário.")
                return

            p = Path(path)
            if not p.exists():
                QMessageBox.warning(self, "YAML não encontrado", f"O arquivo não existe:\n{path}")
                return

            self.yaml_path = str(p)
            self._official_mode = False
            self._only_yaml_actions = True
            logger.info(f"[GuideUI] YAML escolhido no Guia: {self.yaml_path}")

            try:
                parent = self.parent()
                if parent is not None:
                    if hasattr(parent, "current_yaml_path"):
                        parent.current_yaml_path = p
                    if hasattr(parent, "_yaml_selected_by_user"):
                        parent._yaml_selected_by_user = True
                    if hasattr(parent, "_append_log"):
                        parent._append_log(f"[Guide] YAML selecionado no guia: {p}")
            except Exception as e:
                logger.warning(f"[GuideUI] Falha ao sincronizar com MainWindow: {e}")

            self._update_yaml_header_label()
            self._load_steps_async()

        except Exception as e:
            logger.error(f"[GuideUI] Erro ao escolher YAML no Guia: {e}")
            QMessageBox.critical(self, "Erro", f"Falha ao escolher/carregar o YAML:\n{e}")

    def _filter_only_yaml_steps(self, steps: list[dict]) -> list[dict]:
        """
        Remove passos oficiais (infra/diagnóstico/prepare/check/capture) e
        mantém apenas ações vindas do YAML (ex.: scan_, brute_, dos_, custom_).
        """
        try:
            OFFICIAL_IDS = {
                "preflight", "up_vms",
                "attacker_prepare", "sensor_prepare",
                "attacker_tools_check", "sensor_tools_check",
                "connectivity", "sensor_capture_show",
                "hydra_lists"
            }

            filtered = []
            for st in steps:
                sid = (st.get("id") or "").strip()
                tags = [str(t).lower() for t in (st.get("tags") or [])]

                if sid in OFFICIAL_IDS:
                    continue
                if any(t in ("infra", "diagnostic", "safety") for t in tags):
                    continue

                # opcional: se quiser excluir qualquer passo de "captura"
                # if any(t in ("capture", "sensor") for t in tags):
                #     continue

                filtered.append(st)

            logger.info(f"[Guide] only_yaml: {len(filtered)}/{len(steps)} passos após filtro")
            return filtered or steps
        except Exception as e:
            logger.warning(f"[Guide] only_yaml filtro falhou: {e}")
            return steps

    def _reload_official(self):
        try:
            logger.info("[Guide] Recarregar oficial solicitado")
            self._ignore_loader_results = False
            self._rendered_real = False
            self._official_mode = True
            self._only_yaml_actions = False
            self.lbl_yaml.setText("oficial (sem YAML)")
            self.lbl_yaml.setToolTip("Modo oficial (parser) sem YAML selecionado.")
            self.lbl_footer.setText("Recarregando (parser oficial)…")
            self._load_steps_async()
        except Exception as e:
            logger.error(f"[Guide] reload oficial falhou: {e}")

    # -----------------------------
    # Runner (dataset) — NOVO
    # -----------------------------
    def _run_runner_async(self):
        logger.warning("[Guide] Runner solicitado (Gerar dataset)")
        self.console.appendPlainText("[guide] Iniciando Runner com o YAML atual…")

        def job():
            try:
                try:
                    from app.core import runner as core_runner
                    if hasattr(core_runner, "run_from_yaml"):
                        return core_runner.run_from_yaml(self.yaml_path)
                    if hasattr(core_runner, "main"):
                        return core_runner.main(["--yaml", self.yaml_path])
                except Exception as e:
                    logger.warning(f"[Guide] import app.core.runner falhou: {e}")
                cmd = [sys.executable, "-m", "app.core.runner", "--yaml", self.yaml_path]
                proc = subprocess.run(cmd, capture_output=True, text=True)
                return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
            except Exception as e:
                raise RuntimeError(f"Runner falhou: {e}")

        def ok(res):
            try:
                if isinstance(res, dict):
                    out = res.get("stdout", "")
                    err = res.get("stderr", "")
                    rc  = res.get("returncode", 0)
                    if out: self.console.appendPlainText(out)
                    if err: self.console.appendPlainText(err)
                    self.console.appendPlainText(f"[guide] Runner finalizado (rc={rc}).")
                else:
                    self.console.appendPlainText("[guide] Runner finalizado.")
            except Exception as e:
                logger.warning(f"[Guide] erro output runner: {e}")
            self.lbl_footer.setText("Dataset gerado (verifique a pasta data/).")

        def fail(msg: str):
            logger.error(f"[Guide] Runner falhou: {msg}")
            self.console.appendPlainText(f"[erro] Runner: {msg}")
            QMessageBox.critical(self, "Runner", f"Falha: {msg}")

        w = _FnWorker(job)
        self._keep_worker(w)
        w.result.connect(ok)
        w.error.connect(fail)
        w.finished.connect(lambda: self._cleanup_worker(w))
        w.start()

    # -----------------------------
    # Loading com watchdog não letal
    # -----------------------------
    def _start_loading_with_watchdog(self):
        _here("Dialog._start_loading_with_watchdog")
        try:
            self._load_steps_async()

            self._watchdog = QTimer(self)
            self._watchdog.setSingleShot(True)
            self._watchdog.timeout.connect(self._on_loading_slow)
            self._watchdog.start(5000)

            self._watchdog2 = QTimer(self)
            self._watchdog2.setSingleShot(True)
            self._watchdog2.timeout.connect(self._on_loading_very_slow)
            self._watchdog2.start(20000)

            self.lbl_footer.setText("Carregando passos do guia (parser oficial)…")
        except Exception as e:
            logger.error(f"[Guide] start_loading_with_watchdog erro: {e}", exc_info=True)

    def _load_steps_async(self):
        _here("Dialog._load_steps_async:start")
        def job():
            logger.warning(f"[Guide][Loader] parse_yaml_to_steps (oficial) yaml={self.yaml_path}")
            return parse_yaml_to_steps(self.yaml_path or "", self.ssh)
        w = _FnWorker(job)
        self._loader_worker = w
        w.result.connect(self._on_loader_ok)
        w.error.connect(self._on_loader_err)
        w.finished.connect(lambda: self._cleanup_worker(w))
        self._keep_worker(w)
        w.start()
        _here("Dialog._load_steps_async:end")

    def _on_loading_slow(self):
        logger.warning("[Guide] Watchdog fase-1: oficial está demorando.")
        if self._rendered_real or self._rendered_fallback:
            logger.warning("[Guide] Watchdog fase-1 ignorado (já renderizou algo).")
            return
        try:
            if getattr(self, "_loading_spinner", None):
                try:
                    self._loading_spinner.base = "Carregando (parser oficial demorando…)"
                except Exception:
                    self._loading_spinner.start("Carregando (parser oficial demorando…)")
            if getattr(self, "_loading_label", None):
                self._loading_label.setText("Carregando passos… (parser oficial está processando)")
            self.lbl_footer.setText("Aguarde: obtendo passos oficiais (rede/SSH/VMs podem influenciar).")
        except Exception as e:
            logger.warning(f"[Guide] slow info erro: {e}")

    def _on_loading_very_slow(self):
        logger.warning("[Guide] Watchdog fase-2: ainda sem resultado; oferecendo guia básico.")
        if self._rendered_real or self._rendered_fallback:
            logger.warning("[Guide] Watchdog fase-2 ignorado (já renderizou algo).")
            return
        try:
            self.btn_show_basic.setVisible(True)
            self.lbl_footer.setText(
                "Parser oficial ainda carregando… Você pode abrir o 'Guia básico agora' enquanto isso."
            )
        except Exception as e:
            logger.warning(f"[Guide] very slow erro: {e}")

    def _show_basic_fallback(self):
        logger.warning("[Guide] Usuário solicitou guia básico enquanto oficial carrega.")
        if self._rendered_real:
            logger.warning("[Guide] Oficial já renderizado — ignorando guia básico.")
            return
        try:
            steps = self._naive_parse_yaml(self.yaml_path)
        except Exception as e:
            logger.error(f"[Guide] Fallback falhou: {e}", exc_info=True)
            steps = [{
                "id": "fallback_error",
                "title": "Falha no fallback",
                "description": f"Erro ao analisar YAML local: {e}",
                "command": "",
                "host": "attacker",
                "tags": ["fallback"],
                "eta": "",
                "artifacts": []
            }]
        self._render_steps(steps, replace=True)
        self._rendered_fallback = True
        self._ignore_loader_results = False
        self.btn_show_basic.setVisible(False)
        self.lbl_footer.setText("Guia básico exibido — parser oficial substituirá ao concluir.")

    def _on_loader_ok(self, steps: list[dict]):
        logger.warning(f"[Guide] Parser oficial retornou {len(steps)} passos")
        if self._ignore_loader_results:
            logger.warning("[Guide] Ignorando resultados do parser oficial (flag ativa).")
            return
        if (not self._official_mode) and getattr(self, "_only_yaml_actions", False):
            try:
                steps = self._filter_only_yaml_steps(steps)
                self.lbl_footer.setText("Passos prontos (apenas ações do YAML).")
            except Exception as e:
                logger.warning(f"[Guide] falha ao filtrar only_yaml: {e}")
                self.lbl_footer.setText("Passos prontos (parser oficial).")
        self._render_steps(steps, replace=True)
        self._rendered_real = True
        self.lbl_footer.setText("Passos prontos (parser oficial).")

    def _on_loader_err(self, msg: str):
        logger.error(f"[Guide] Parser oficial falhou: {msg}")
        if not self._rendered_fallback:
            self._render_steps([{
                "id": "fallback_error",
                "title": "Falha ao carregar YAML",
                "description": msg,
                "command": "",
                "host": "attacker",
                "tags": ["erro"],
                "eta": "",
                "artifacts": []
            }], replace=True)
        self.lbl_footer.setText("Falha ao carregar o guia.")

    def _render_steps(self, steps: list[dict], replace: bool):
        _here(f"Dialog._render_steps replace={replace} count={len(steps)}")
        try:
            try:
                if getattr(self, "_watchdog", None): self._watchdog.stop()
                if getattr(self, "_loading_spinner", None): self._loading_spinner.stop("")
                if getattr(self, "_loading_label", None):
                    self.cards_layout.removeWidget(self._loading_label)
                    self._loading_label.deleteLater()
                if getattr(self, "_watchdog2", None):
                    self._watchdog2.stop()
            except Exception as e:
                logger.warning(f"[Guide] limpar placeholder: {e}")

            if replace:
                removed = 0
                while self.cards_layout.count():
                    item = self.cards_layout.takeAt(0)
                    w = item.widget()
                    if w is not None:
                        w.setParent(None)
                        w.deleteLater()
                        removed += 1
                self.cards.clear()
                logger.warning(f"[Guide] cards removidos do layout: {removed}")

            for i, st in enumerate(steps, start=1):
                logger.warning(f"[Guide] adicionando card {i}: id={st.get('id')} title={st.get('title')}")
                card = StepCard(i, st)
                card.run_clicked.connect(self._run_step_async)
                card.ssh_clicked.connect(lambda host, cmd, c=card: self._ssh_exec_or_paste(host, cmd, c))
                card.mark_done.connect(lambda s: self._mark_timeline(s, "done"))
                self.cards_layout.addWidget(card)
                self.cards.append(card)
            spacer = QWidget()
            spacer.setFixedHeight(6)
            self.cards_layout.addWidget(spacer)
            logger.warning(f"[Guide] total de cards agora: {len(self.cards)}")
        except Exception as e:
            logger.error(f"[Guide] _render_steps falhou: {e}", exc_info=True)

    # -----------------------------
    # Execução (streaming)
    # -----------------------------
    def _append_console(self, text: str):
        try:
            self.console.appendPlainText(text)
        except Exception as e:
            logger.warning(f"[Guide] append console falhou: {e}")

    def _find_card(self, step: dict) -> StepCard | None:
        for c in self.cards:
            if c.step is step or (c.step.get("id") and c.step.get("id") == step.get("id")):
                return c
        return None

    def _ssh_exec_or_paste(self, host: str, cmd: str, card: 'StepCard'):
        """
        Abre/reutiliza SSH e (se houver) envia o comando via tmux — sem bloquear a UI.
        Usa _FnWorker para isolar as chamadas potencialmente lentas (status/wait_ssh/ssh).
        """
        try:
            base_host = (host or "attacker").strip().lower()
            self._append_console(f"[guide] Preparando SSH para '{base_host}'…")

            def job():
                # 1) Se a MainWindow oferece _ssh_paste, use-a (mas agora em thread)
                parent = self.parent()
                if parent is not None and hasattr(parent, "_ssh_paste"):
                    parent._ssh_paste(base_host, cmd or "")
                    return f"ssh_paste:{base_host}"

                # 2) Fallback local (mesma lógica de _open_ssh_from_card, porém sem UI calls)
                st = self.vagrant.status_by_name(base_host)
                if st != "running":
                    return {"warn": f"{base_host} não está 'running' (rode: vagrant up {base_host})."}

                # Pode demorar – mantemos fora da UI
                self.vagrant.wait_ssh_ready(base_host, str(self.lab_dir), attempts=10, delay_s=3)

                # Abre o terminal externo
                self.ssh.open_external_terminal(base_host)

                # tmux opcional + paste do comando
                try:
                    session = f"guide_{base_host}"
                    self.ssh.run_command(base_host, f"tmux new-session -d -s {session} || true", timeout=20)
                    payload = (cmd or "").strip()
                    if payload:
                        import shlex
                        quoted = shlex.quote(payload.replace("\r\n", "\n"))
                        self.ssh.run_command(base_host, f"tmux send-keys -t {session} {quoted} C-m", timeout=20)
                    return f"fallback_tmux:{base_host}"
                except Exception as e:
                    return {"warn": f"SSH aberto (sem tmux) — envie manualmente. Detalhe: {e}"}

            def ok(res):
                try:
                    if isinstance(res, dict) and "warn" in res:
                        self._append_console(f"[warn] {res['warn']}")
                        card.set_ssh_done(res["warn"])
                        self.lbl_footer.setText(res["warn"])
                    else:
                        self._append_console(f"[guide] SSH ativo em {host}. Comando (se fornecido) foi enviado.")
                        card.set_ssh_done("Comando enviado via SSH ✓")
                        self.lbl_footer.setText(f"SSH ativo em {host}.")
                except Exception:
                    pass

            def fail(msg: str):
                logger.error(f"[Guide] _ssh_exec_or_paste async falhou: {msg}", exc_info=False)
                card.set_ssh_done("Falha ao enviar comando via SSH ✖")
                try:
                    QMessageBox.warning(self, "SSH", f"Falha: {msg}")
                except Exception:
                    pass

            w = _FnWorker(job)
            self._keep_worker(w)
            w.result.connect(ok)
            w.error.connect(fail)
            w.finished.connect(lambda: self._cleanup_worker(w))
            w.start()

        except Exception as e:
            logger.error(f"[Guide] _ssh_exec_or_paste falhou: {e}", exc_info=True)
            card.set_ssh_done("Falha ao enviar comando via SSH ✖")
            try:
                QMessageBox.warning(self, "SSH", f"Falha: {e}")
            except Exception:
                pass

    def _open_ssh_from_card(self, host: str, card: StepCard | None = None):
        """
        Abre um terminal SSH externo para 'host' com todas as salvaguardas.
        1) Prioriza a lógica centralizada da MainWindow (_ssh), que já valida estado e SSH-ready.
        2) Se a MainWindow não estiver disponível, aplica fallback seguro local.
        """
        try:
            host = (host or "attacker").strip().lower()
            host = {"vitima": "victim"}.get(host, host)

            if self._stream_worker and self._stream_worker.isRunning():
                self._append_console(
                    "[guide] Aviso: há um passo em execução; abrir SSH pode interferir na leitura do console.")

            try:
                parent = self.parent()
                if parent is not None and hasattr(parent, "_ssh"):
                    self._append_console(f"[guide] Solicitando SSH seguro via tela principal para '{host}'…")
                    parent._ssh(host)
                    self.lbl_footer.setText(f"Abrindo SSH para {host}…")
                    return
            except Exception as e:
                logger.warning(f"[Guide] MainWindow._ssh indisponível: {e}")

            try:
                st = self.vagrant.status_by_name(host)
                if st != "running":
                    self._append_console(f"[warn] {host} não está 'running'. Rode: vagrant up {host}.")
                    QMessageBox.warning(self, "SSH", f"{host} não está 'running'.")
                    return
            except Exception as e:
                self._append_console(f"[erro] Falha ao consultar status de {host}: {e}")
                QMessageBox.critical(self, "SSH", f"Falha ao consultar status: {e}")
                return

            try:
                self.vagrant.wait_ssh_ready(host, str(self.lab_dir), attempts=10, delay_s=3)
                self._append_console(f"[guide] {host} com SSH pronto.")
            except Exception as e:
                self._append_console(f"[warn] SSH ainda não pronto em {host}: {e}")
                QMessageBox.warning(self, "SSH", f"SSH não respondeu ainda em {host}: {e}")
                return

            try:
                self._append_console(f"[guide] Abrindo terminal SSH externo para {host}…")
                self.ssh.open_external_terminal(host)
                self.lbl_footer.setText(f"Terminal SSH aberto para {host}.")
            except Exception as e:
                self._append_console(f"[erro] Falha ao abrir SSH externo: {e}")
                QMessageBox.critical(self, "SSH", f"Falha ao abrir terminal: {e}")

        except Exception as e:
            logger.error(f"[Guide] _open_ssh_from_card falhou: {e}", exc_info=True)
            try:
                if card: card.set_idle()
            except Exception:
                pass
            QMessageBox.critical(self, "SSH", f"Erro: {e}")

    def _run_step_async(self, step: dict):
        host = step.get("host", "attacker")
        cmd = (step.get("command", "") or "").strip()
        logger.info(f"[Guide] run_step_async host={host} cmd_len={len(cmd)} step_id={step.get('id')}")

        if not cmd:
            QMessageBox.warning(self, "Sem comando", "Este passo não definiu um comando executável.")
            return

        # Tentativa opcional de substituir placeholders {attacker_ip}/{victim_ip}/{sensor_ip}
        try:
            if any(tok in cmd for tok in ("{attacker_ip}", "{victim_ip}", "{sensor_ip}")):
                try:
                    from app.core.yaml_parser import resolve_guest_ips, substitute_vars
                    ips = resolve_guest_ips(self.ssh)
                    cmd = substitute_vars(cmd, ips)
                    logger.info(f"[Guide] Placeholders de IP substituídos com sucesso: {ips}")
                except Exception as e:
                    logger.warning(
                        f"[Guide] Não foi possível resolver IPs agora (seguindo com o comando original): {e}")
        except Exception as e:
            logger.warning(f"[Guide] Substituição de placeholders falhou: {e}")

        if self._stream_worker and self._stream_worker.isRunning():
            self._append_console("[guide] Encerrando stream anterior…")
            self._cancel_running(wait_worker=True)

        card = self._find_card(step)
        if card:
            card.set_running()
        self._mark_timeline(step, "start")
        self._append_console("")
        self._append_console(f"=== PASSO: {step.get('title', '(sem título)')} | host={host} ===")
        self._append_console(f"$ {cmd}")

        timeout = int(step.get("timeout", 600))
        w = _StreamWorker(self.ssh, host, cmd, timeout_s=timeout)
        self._stream_worker = w
        w.line.connect(self._append_console)
        w.finished_ok.connect(lambda: self._on_step_done(card, step, ok=True))
        w.error.connect(lambda msg: self._on_step_fail(card, step, msg))
        w.finished.connect(lambda: (self._on_step_final(card, step), self._cleanup_worker(w)))
        self._keep_worker(w)
        self.lbl_footer.setText(f"Executando passo em {host}…")
        logger.info(f"[Guide] iniciando {getattr(w, 'id', '_StreamWorker')}")
        w.start()

    def _on_step_done(self, card: StepCard | None, step: dict, ok: bool):
        logger.warning(f"[Guide] passo concluído ok={ok} step_id={step.get('id')}")
        try:
            if card:
                card.status.setText("Concluído ✓" if ok else "Finalizado")
                card._set_status_state("done")
            self._append_console("[guide] Passo concluído.")
        except Exception:
            pass

    def _on_step_fail(self, card: StepCard | None, step: dict, msg: str):
        logger.error(f"[Guide] passo falhou: {msg}")
        try:
            if card:
                card.status.setText("Falhou ✖")
                card._set_status_state("error")
            self._append_console(f"[erro] {msg}")
            QMessageBox.critical(self, "Erro no passo", msg)
        except Exception as e:
            logger.error(f"[Guide] erro exibindo falha do passo: {e}")

    def _on_step_final(self, card: StepCard | None, step: dict):
        logger.warning(f"[Guide] passo finalizado step_id={step.get('id')}")
        try:
            if card: card.set_idle()
            self._mark_timeline(step, "end")
            self._write_timeline()

            try:
                if self._batch_running:
                    if self._batch_queue:
                        self.console.appendPlainText("[guide] Próximo passo em 0.15s…")
                        QTimer.singleShot(150, lambda: self._run_step_async(self._batch_queue.pop(0)))
                    else:
                        self._batch_running = False
                        self.console.appendPlainText("[guide] Lote concluído ✓")
                        self.lbl_footer.setText("Execução em lote concluída.")
            except Exception as e:
                logger.warning(f"[Guide] batch-next falhou: {e}")
        except Exception:
            pass

    def _cancel_running(self, wait_worker: bool = False):
        logger.warning(f"[Guide] cancel_running wait={wait_worker}")
        try:
            if hasattr(self.ssh, "cancel_all_running"):
                try:
                    self.ssh.cancel_all_running()
                    logger.warning("[Guide] ssh.cancel_all_running enviado")
                except Exception as e:
                    logger.warning(f"[Guide] cancel_all_running falhou: {e}")
            if self._stream_worker and self._stream_worker.isRunning():
                self._stream_worker.stop()
                if wait_worker:
                    self._stream_worker.wait(2000)
            self._append_console("[guide] Cancelamento solicitado.")
            self.lbl_footer.setText("Cancelamento solicitado.")
            self._batch_running = False
            self._batch_queue = []
        except Exception as e:
            logger.error(f"[Guide] cancel falhou: {e}")
            QMessageBox.warning(self, "Cancelar", f"Falha ao cancelar: {e}")

    def _save_console_to_file(self):
        try:
            default = str((self.project_root / ".meta" / f"{Path(self.yaml_path).stem}_console.log").resolve())
            Path(default).parent.mkdir(parents=True, exist_ok=True)
            path, _ = QFileDialog.getSaveFileName(self, "Salvar log do console", default, "Log (*.log);;Texto (*.txt)")
            if not path:
                logger.warning("[Guide] salvar console cancelado pelo usuário")
                return
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.console.toPlainText())
            self.lbl_footer.setText(f"Log salvo em: {path}")
            logger.warning(f"[Guide] console salvo em {path}")
        except Exception as e:
            logger.error(f"[Guide] salvar console falhou: {e}")
            QMessageBox.warning(self, "Salvar log", f"Falha ao salvar: {e}")

    # -----------------------------
    # Linha do tempo
    # -----------------------------
    def _mark_timeline(self, step, phase):
        sid = step.get("id") or f"step_{int(time.time())}"
        self.timeline.setdefault(sid, {})
        self.timeline[sid][phase] = time.time()
        logger.warning(f"[Guide] timeline[{sid}]['{phase}'] = now")

    def _write_timeline(self):
        try:
            meta_dir = self.project_root / ".meta"
            meta_dir.mkdir(parents=True, exist_ok=True)
            f = meta_dir / (Path(self.yaml_path).stem + "_timeline.json")
            f.write_text(json.dumps(self.timeline, indent=2), encoding="utf-8")
            logger.warning(f"[Guide] timeline gravada em {f}")
        except Exception as e:
            logger.warning(f"[Guide] persist timeline falhou: {e}")

    # -----------------------------
    # Isolamento (async)
    # -----------------------------
    def _toggle_isolation_async(self):
        logger.warning("[Guide] isolamento solicitado (egress guard)")
        def job():
            from lab.security.safety import toggle_attacker_nat
            return toggle_attacker_nat(self.ssh, enable=False)
        def ok(_):
            self.lbl_footer.setText("Atacante isolado. (Use o botão novamente no app principal para remover)")
            self.console.appendPlainText("[guide] Isolamento aplicado.")
            logger.warning("[Guide] isolamento aplicado")
        def fail(msg: str):
            logger.error(f"[Guide] isolamento falhou: {msg}")
            QMessageBox.critical(self, "Isolamento", f"Falha: {msg}")

        w = _FnWorker(job)
        w.result.connect(ok)
        w.error.connect(fail)
        w.finished.connect(lambda: self._cleanup_worker(w))
        self._keep_worker(w)
        w.start()

    # -----------------------------
    # Gestão de workers
    # -----------------------------
    def _keep_worker(self, w: QThread):
        try:
            self._workers.add(w)
            logger.warning(f"[Guide] worker keep={getattr(w,'id',type(w).__name__)} total={len(self._workers)}")
            w.finished.connect(lambda: self._workers.discard(w))
        except Exception as e:
            logger.warning(f"[Guide] keep worker: {e}")

    def _cleanup_worker(self, w: QThread):
        try:
            self._workers.discard(w)
            logger.warning(f"[Guide] worker cleanup={getattr(w,'id',type(w).__name__)} total={len(self._workers)}")
        except Exception:
            pass

    def reject(self):
        logger.warning("[Guide] Dialog.reject() — fechamento solicitado pelo usuário")
        try:
            if self._workers:
                self._cancel_running()
        except Exception:
            pass
        super().reject()

    # -----------------------------
    # Fallback robusto
    # -----------------------------
    def _naive_parse_yaml(self, yaml_path: str) -> list[dict]:
        logger.warning(f"[Guide] naive_parse_yaml: {yaml_path}")
        # 1) Guard clause para vazio/diretório/arquivo inexistente
        try:
            p = Path(yaml_path) if yaml_path else None
            if (not yaml_path) or (p and (not p.exists() or p.is_dir())):
                logger.warning("[Guide] naive_parse_yaml: sem YAML válido — usando fallback oficial.")
                return [{
                    "id": "fallback_official",
                    "title": "Modo oficial (sem YAML)",
                    "description": (
                        "Nenhum YAML selecionado. Use 'Recarregar (oficial)' para carregar os passos padrão "
                        "ou escolha um YAML para ver ações específicas (scan/brute/DoS)."
                    ),
                    "command": "",
                    "host": "attacker",
                    "tags": ["fallback", "official"],
                    "eta": "",
                    "artifacts": []
                }]
        except Exception as e:
            logger.warning(f"[Guide] naive_parse_yaml guard falhou: {e}")

        # 2) Parsing local rápido (apenas quando é realmente um arquivo)
        try:
            try:
                import yaml
                data = yaml.safe_load(Path(yaml_path).read_text(encoding="utf-8")) or {}
                logger.warning("[Guide] naive_parse_yaml: PyYAML ok")
            except Exception as e:
                logger.warning(f"[Guide] naive_parse_yaml sem PyYAML/erro={e} — heurística simples")
                txt = Path(yaml_path).read_text(encoding="utf-8")
                data = {}
                if "actions:" in txt:
                    data["actions"] = [{"name": "executar_experimento", "params": {}}]
                else:
                    data["actions"] = []
        except Exception as e:
            raise RuntimeError(f"Fallback: falha ao ler YAML: {e}")

        steps = []
        actions = data.get("actions") or []
        logger.warning(f"[Guide] naive_parse_yaml: actions={len(actions)}")
        for i, act in enumerate(actions, start=1):
            name = (act.get("name") or f"acao_{i}").strip()
            params = act.get("params") or {}
            host = params.get("host") or (
                "attacker" if any(k in name.lower() for k in ("scan", "brute", "dos")) else "sensor")
            cmd_hint = f"# execute '{name}' com params={params}"
            steps.append({
                "id": f"fallback_{i}",
                "title": name,
                "description": "Fallback simples: parsing local sem consultas remotas.",
                "command": cmd_hint,
                "host": host,
                "tags": ["fallback"],
                "eta": "",
                "artifacts": []
            })
        if not steps:
            steps = [{
                "id": "fallback_empty",
                "title": "Sem ações no YAML",
                "description": "O arquivo não tem 'actions'.",
                "command": "",
                "host": "attacker",
                "tags": ["fallback"],
                "eta": "",
                "artifacts": []
            }]
        return steps

    def _load_theme(self):
        try:
            qss = (Path(__file__).parent / "futuristic.qss").read_text(encoding="utf-8")
            self.setStyleSheet(qss)
        except Exception as e:
            logger.warning(f"[Guide] load_theme falhou: {e}")
