import logging, time, json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, List
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QWidget, QFrame, QMessageBox, QPlainTextEdit, QFileDialog
)
from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, Signal, QThread
from PySide6.QtGui import QGuiApplication, QClipboard

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
        logger.warning(f"[Guide][{self.id}] fallback _run_no_stream classic={use_classic}")
        out = (
            self.ssh.run_command(self.host, f"bash -lc '{self.cmd}'", timeout=self.timeout_s)
            if use_classic else
            self.ssh.run_command_cancellable(self.host, self.cmd, timeout_s=self.timeout_s)
        )
        self._emit_block_output(out)

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

        cmd = self.step.get("command","")
        cmd_box = QPlainTextEdit(cmd)
        cmd_box.setReadOnly(True)
        cmd_box.setFixedHeight(64)
        cmd_box.setObjectName("GuideCmd")

        art = self.step.get("artifacts", [])
        art_label = QLabel("Artefatos esperados: " + (", ".join(art) if art else "—"))
        art_label.setObjectName("GuideArtifacts")
        art_label.setWordWrap(True)

        row = QHBoxLayout()
        btn_copy = QPushButton("Copiar")
        btn_run  = QPushButton(f"Rodar no {self.step.get('host','guest')}")
        btn_done = QPushButton("Marcar ✓")
        self.status = QLabel("A fazer")
        self.status.setObjectName("GuideStatus")

        btn_copy.clicked.connect(lambda: self._on_copy(cmd_box.toPlainText()))
        btn_run.clicked.connect(lambda: self._emit_run(self.step))
        btn_done.clicked.connect(lambda: self._on_done())

        row.addWidget(btn_copy)
        row.addWidget(btn_run)
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
        logger.warning(f"[Guide][{self.cid}] DONE marcado")
        self.status.setText("Concluído ✓")
        self.mark_done.emit(self.step)

    def set_running(self):
        logger.warning(f"[Guide][{self.cid}] set_running()")
        self.status.setText("Executando…")
        self._spin = getattr(self, "_spin", None)
        if not self._spin:
            self._spin = _MiniSpinner(self.status, "Executando")
        self._spin.start()

    def set_idle(self):
        logger.warning(f"[Guide][{self.cid}] set_idle()")
        if hasattr(self, "_spin") and self._spin:
            try:
                self._spin.stop("A fazer")
            except Exception:
                self.status.setText("A fazer")

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
    def __init__(self, parent, yaml_path: str, ssh, vagrant, lab_dir: str, project_root: str):
        super().__init__(parent)
        try:
            self._apply_window_flags()
        except Exception as e:
            logger.warning(f"[Guide] _apply_window_flags erro: {e}")

        _here("Dialog.__init__:start")

        self.setObjectName("GuideDialog")
        try:
            self.yaml_path = str(Path(yaml_path).resolve())
        except Exception as e:
            logger.warning(f"[Guide] resolve do yaml_path falhou: {e}")
            self.yaml_path = yaml_path

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

        self._rendered_fallback = False
        self._rendered_real = False
        self._ignore_loader_results = False

        logger.warning(f"[Guide][Dialog] yaml={self.yaml_path} lab_dir={self.lab_dir} project_root={self.project_root}")
        logger.warning(f"[Guide][Dialog] parent={_safe(parent)} ssh={_safe(type(self.ssh))} vagrant={_safe(type(self.vagrant))}")

        self._build_ui()

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
        self.lbl_yaml = QLabel(Path(self.yaml_path).name)
        self.lbl_yaml.setObjectName("GuideYaml")
        self.lbl_yaml.setToolTip(self.yaml_path)
        self.btn_reload = QPushButton("Recarregar (oficial)")
        self.btn_reload.clicked.connect(self._reload_official)
        header.addWidget(self.lbl_title)
        header.addStretch(1)
        header.addWidget(self.lbl_yaml)
        header.addWidget(self.btn_reload)

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

    def _reload_official(self):
        try:
            logger.warning("[Guide] Recarregar oficial solicitado")
            self._ignore_loader_results = False
            self._rendered_real = False
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
            self._watchdog.start(6000)
            self.lbl_footer.setText("Carregando passos do YAML…")
        except Exception as e:
            logger.error(f"[Guide] start_loading_with_watchdog erro: {e}", exc_info=True)

    def _load_steps_async(self):
        _here("Dialog._load_steps_async:start")
        def job():
            logger.warning(f"[Guide][Loader] parse_yaml_to_steps (oficial) yaml={self.yaml_path}")
            return parse_yaml_to_steps(self.yaml_path, self.ssh, self.vagrant)
        w = _FnWorker(job)
        self._loader_worker = w
        w.result.connect(self._on_loader_ok)
        w.error.connect(self._on_loader_err)
        w.finished.connect(lambda: self._cleanup_worker(w))
        self._keep_worker(w)
        w.start()
        _here("Dialog._load_steps_async:end")

    def _on_loading_slow(self):
        logger.warning("[Guide] Watchdog disparou — exibindo fallback simples.")
        if self._rendered_real or self._rendered_fallback:
            logger.warning("[Guide] Watchdog ignorado (já renderizou algo).")
            return
        try:
            steps = self._naive_parse_yaml(self.yaml_path)
            logger.warning(f"[Guide] Fallback montou {len(steps)} passos")
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
        self._ignore_loader_results = True
        self.lbl_footer.setText("Fallback ativo — clique em ‘Recarregar (oficial)’ quando quiser.")

    def _on_loader_ok(self, steps: list[dict]):
        logger.warning(f"[Guide] Parser oficial retornou {len(steps)} passos")
        if self._ignore_loader_results:
            logger.warning("[Guide] Ignorando resultados do parser oficial (flag ativa).")
            return
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
            # parar spinner
            try:
                if getattr(self, "_watchdog", None): self._watchdog.stop()
                if getattr(self, "_loading_spinner", None): self._loading_spinner.stop("")
                if getattr(self, "_loading_label", None):
                    self.cards_layout.removeWidget(self._loading_label)
                    self._loading_label.deleteLater()
            except Exception as e:
                logger.warning(f"[Guide] limpar placeholder: {e}")

            if replace:
                # remove antigos
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

            # adiciona novos
            for i, st in enumerate(steps, start=1):
                logger.warning(f"[Guide] adicionando card {i}: id={st.get('id')} title={st.get('title')}")
                card = StepCard(i, st)
                card.run_clicked.connect(self._run_step_async)
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

    def _run_step_async(self, step: dict):
        host = step.get("host","attacker")
        cmd  = (step.get("command","") or "").strip()
        logger.warning(f"[Guide] run_step_async host={host} cmd_len={len(cmd)} step_id={step.get('id')}")
        if not cmd:
            QMessageBox.warning(self, "Sem comando", "Este passo não definiu um comando executável.")
            return
        if self._stream_worker and self._stream_worker.isRunning():
            self._append_console("[guide] Encerrando stream anterior…")
            self._cancel_running(wait_worker=True)

        card = self._find_card(step)
        if card: card.set_running()
        self._mark_timeline(step, "start")
        self._append_console("")
        self._append_console(f"=== PASSO: {step.get('title','(sem título)')} | host={host} ===")
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
        logger.warning(f"[Guide] iniciando {getattr(w, 'id', '_StreamWorker')}")
        w.start()

    def _on_step_done(self, card: StepCard | None, step: dict, ok: bool):
        logger.warning(f"[Guide] passo concluído ok={ok} step_id={step.get('id')}")
        try:
            if card: card.status.setText("Concluído ✓" if ok else "Finalizado")
            self._append_console("[guide] Passo concluído.")
        except Exception:
            pass

    def _on_step_fail(self, card: StepCard | None, step: dict, msg: str):
        logger.error(f"[Guide] passo falhou: {msg}")
        try:
            if card: card.status.setText("Falhou ✖")
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
            host = params.get("host") or ("attacker" if any(k in name.lower() for k in ("scan","brute","dos")) else "sensor")
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
