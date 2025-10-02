# -*- coding: utf-8 -*-
from __future__ import annotations
import json, logging, time, sys, subprocess
from pathlib import Path
from typing import List, Dict

from PySide6.QtCore import Qt, QTimer, Signal, QThread
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QWidget, QPlainTextEdit,
    QFileDialog, QMessageBox, QProgressBar, QApplication
)

from app.core.yaml_parser import parse_yaml_to_steps
from app.ui.guide.step_card_widget import StepCard, role_from_host
from app.ui.guide.spinner import _MiniSpinner
from app.ui.guide.workers.workers import _FnWorker, _StreamWorker
from app.ui.guide.guide_utils import naive_yaml_quick_parse

logger = logging.getLogger("[Guide]")
if not hasattr(logger, "warn"):
    logger.warn = logger.warning


class ExperimentGuideDialog(QDialog):
    """Janela principal do Guia (Matrix/Holo Edition) sincronizada com os ícones do MachineCard."""

    guide_loaded = Signal(int)        # nº de passos carregados
    guide_failed = Signal(str)        # msg de erro ao carregar
    batch_started = Signal(int)       # nº de passos em lote
    batch_finished = Signal()         # fim do lote

    def __init__(self, yaml_path: str, ssh, vagrant, lab_dir: str, project_root: str, parent=None):
        super().__init__(parent)
        self.first_show = True
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self._load_theme()

        self.yaml_path = str(Path(yaml_path).resolve()) if yaml_path else ""
        try:
            if Path(self.yaml_path).is_dir():
                self.yaml_path = ""  # força modo oficial
        except Exception:
            pass

        self._official_mode = (not self.yaml_path) or (not Path(self.yaml_path).exists())
        self._only_yaml_actions = bool(self.yaml_path) and (not self._official_mode)

        self.ssh = ssh
        self.vagrant = vagrant
        self.lab_dir = Path(lab_dir)
        self.project_root = Path(project_root)

        self.cards: List[StepCard] = []
        self.timeline: Dict[str, dict] = {}
        self._workers: set[QThread] = set()
        self._stream_worker: _StreamWorker | None = None
        self._loader_worker: _FnWorker | None = None
        self._watchdog: QTimer | None = None
        self._watchdog2: QTimer | None = None
        self._rendered_fallback = False
        self._rendered_real = False
        self._ignore_loader_results = False
        self._batch_running = False
        self._cancel_requested = False
        self._current_card = None
        self._current_worker = None
        self._batch_queue: list[dict] = []
        self._last_states: Dict[str, str] = {}


        self._build_ui()
        self._update_yaml_header_label()

        QTimer.singleShot(0, self._start_loading_with_watchdog)

    # ---------- UI ----------
    def _build_ui(self):
        self.setObjectName("GuideDialog")
        self.setWindowTitle("Guia do Experimento — Matrix/Holo")
        self.setMinimumSize(1040, 760)
        self._apply_window_flags()

        main = QVBoxLayout(self)
        main.setContentsMargins(16, 16, 14, 12)
        main.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(10)

        self.lbl_title = QLabel("◤ Guia do Experimento ◢")
        self.lbl_title.setObjectName("GuideHeader")

        self.lbl_yaml = QLabel("—")
        self.lbl_yaml.setObjectName("GuideYaml")

        self.timeline_bar = QProgressBar()
        self.timeline_bar.setObjectName("GuideTimelineBar")
        self.timeline_bar.setRange(0, 100)
        self.timeline_bar.setValue(0)
        self.timeline_bar.setTextVisible(False)
        self.timeline_bar.setFixedHeight(8)

        self.btn_pick_yaml = QPushButton("Escolher YAML…")
        self.btn_reload = QPushButton("Recarregar (oficial)")
        self.btn_clear_tests = QPushButton("Limpar")
        self.btn_run_all = QPushButton("Rodar todos")
        self.btn_mark_all_done = QPushButton("Marcar ✓")

        self.btn_pick_yaml.setToolTip("Selecionar um arquivo YAML de experimento (substitui o oficial)")
        self.btn_reload.setToolTip("Recarregar o parser oficial (ignora qualquer YAML selecionado)")
        self.btn_clear_tests.setToolTip("Limpar console, resetar status dos passos e apagar timeline")
        self.btn_run_all.setToolTip("Executar todos os passos que definiram um comando")
        self.btn_mark_all_done.setToolTip("Marcar todos os passos como concluídos (sem executar)")

        try:
            self.btn_run_all.setEnabled(False)
        except Exception:
            pass

        for b in (self.btn_pick_yaml, self.btn_reload, self.btn_clear_tests, self.btn_run_all, self.btn_mark_all_done):
            b.setObjectName("HoloBtn")

        self.btn_pick_yaml.clicked.connect(self._on_pick_yaml_in_guide)
        self.btn_reload.clicked.connect(self._reload_official)
        self.btn_clear_tests.clicked.connect(self._clear_tests)
        self.btn_run_all.clicked.connect(self._run_all_steps)
        self.btn_mark_all_done.clicked.connect(self._mark_all_done)

        header.addWidget(self.lbl_title)
        header.addStretch(1)
        header.addWidget(self.lbl_yaml)
        header.addSpacing(6)
        header.addWidget(self.btn_pick_yaml)
        header.addWidget(self.btn_reload)
        header.addWidget(self.btn_clear_tests)
        header.addWidget(self.btn_run_all)
        header.addWidget(self.btn_mark_all_done)

        main.addLayout(header)
        main.addWidget(self.timeline_bar)

        # Cards scroll
        self.cards_container = QWidget()
        self.cards_container.setObjectName("GuideCardsArea")
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setContentsMargins(8, 8, 8, 4)
        self.cards_layout.setSpacing(12)

        self._loading_label = QLabel("Preparando parser oficial…")
        self._loading_label.setObjectName("GuideLoading")
        self._loading_spinner = _MiniSpinner(self._loading_label, "Carregando")
        self._loading_spinner.start()
        self.cards_layout.addWidget(self._loading_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.cards_container)
        scroll.setObjectName("GuideScroll")

        main.addWidget(scroll, 1)

        bottom = QHBoxLayout()
        self.btn_console_clear = QPushButton("Limpar console")
        self.btn_console_save = QPushButton("Salvar log…")
        self.btn_isolate = QPushButton("Isolar atacante")
        self.btn_cancel = QPushButton("Cancelar")
        self.btn_run_runner = QPushButton("Gerar dataset")

        self.btn_console_clear.setToolTip("Limpar todo o texto do console abaixo")
        self.btn_console_save.setToolTip("Salvar o conteúdo do console em um arquivo de texto")
        self.btn_isolate.setToolTip("Requer attacker ONLINE")
        self.btn_cancel.setToolTip("Cancelar qualquer passo em execução")
        self.btn_run_runner.setToolTip("Requer pelo menos uma máquina ONLINE")

        for b in (self.btn_console_clear, self.btn_console_save, self.btn_isolate, self.btn_cancel, self.btn_run_runner):
            b.setObjectName("HoloBtn")

        try:
            self.btn_isolate.setEnabled(False)
            self.btn_cancel.setEnabled(False)
            self.btn_run_runner.setEnabled(False)
        except Exception:
            pass

        bottom.addWidget(self.btn_console_clear)
        bottom.addWidget(self.btn_console_save)
        bottom.addStretch(1)
        bottom.addWidget(self.btn_isolate)
        bottom.addWidget(self.btn_cancel)
        bottom.addWidget(self.btn_run_runner)

        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMinimumHeight(220)
        self.console.setObjectName("GuideConsole")

        self.lbl_footer = QLabel("Pronto.")
        self.lbl_footer.setObjectName("GuideFooter")

        main.addLayout(bottom)
        main.addWidget(self.console, 0)
        main.addWidget(self.lbl_footer)

        self.btn_console_clear.clicked.connect(self.console.clear)
        self.btn_console_save.clicked.connect(self._save_console_to_file)
        self.btn_isolate.clicked.connect(self._toggle_isolation_async)
        self.btn_cancel.clicked.connect(lambda: self._cancel_running(wait_worker=True))
        self.btn_run_runner.clicked.connect(self._run_runner_async)

        try:
            self.setWindowOpacity(1.0)
        except Exception as e:
            logger.error(f"[Guide] animação inicial falhou: {e}")

    def _update_footer_actions_enabled(self):
        try:
            states = getattr(self, "_last_states", {}) or {}
            attacker_online = (states.get("attacker") == "running")
            any_online = any(st == "running" for st in states.values()) if states else False
            if hasattr(self, "btn_isolate"):
                self.btn_isolate.setEnabled(attacker_online)
                self.btn_isolate.setToolTip("Requer attacker ONLINE" if not attacker_online else "Isolar atacante")
            if hasattr(self, "btn_run_runner"):
                self.btn_run_runner.setEnabled(any_online)
                self.btn_run_runner.setToolTip(
                    "Requer pelo menos uma máquina ONLINE" if not any_online else "Gerar dataset com o YAML atual")
            if hasattr(self, "btn_cancel"):
                running = (self._stream_worker and self._stream_worker.isRunning()) or self._batch_running
                self.btn_cancel.setEnabled(running)
                self.btn_cancel.setToolTip("Nenhum passo em execução" if not running else "Cancelar execução em andamento")
            if hasattr(self, "btn_run_all"):
                has_commands = any(
                    (c.step.get("command") or "").strip() or (c.step.get("command_normal") or "").strip() or
                    (c.step.get("command_b64") or "").strip()
                    for c in self.cards
                )
                self.btn_run_all.setEnabled(bool(self.cards) and has_commands)
                self.btn_run_all.setToolTip(
                    "Nenhum passo executável" if not has_commands else
                    ("Nenhum passo no guia" if not self.cards else "Executar todos os passos com comando definido"))
        except Exception as e:
            logger.error(f"[Guide] _update_footer_actions_enabled: {e}")

    def showEvent(self, event):
        try:
            if self.first_show:
                self.first_show = False
                try:
                    screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
                    if screen is not None:
                        geo = screen.availableGeometry()
                        self.setGeometry(geo)
                        self.showMaximized()
                except Exception as e:
                    logger.error(f"[UI] showEvent: {e}")
        except Exception as e:
            logger.error(f"[UI] showEvent outer: {e}")

    def _clear_tests(self):
        """Limpa console, reseta status dos cards, zera timeline e apaga o arquivo .meta/*_timeline.json."""
        try:
            logger.info("[Guide] Limpando estado do guia...")
            try:
                self._cancel_running(wait_worker=True)
            except Exception as e:
                logger.error(f"[Guide] _clear_tests.cancel_running: {e}")

            try:
                self.console.clear()
            except Exception as e:
                logger.error(f"[Guide] _clear_tests.console: {e}")

            for c in self.cards:
                try:
                    c.set_idle()
                except Exception as e:
                    logger.error(f"[Guide] _clear_tests.card_idle: {e}")

            try:
                self.timeline.clear()
                self.timeline_bar.setValue(0)
            except Exception as e:
                logger.error(f"[Guide] _clear_tests.timeline_reset: {e}")

            try:
                meta_dir = self.project_root / ".meta"
                tf = meta_dir / (Path(self.yaml_path).stem + "_timeline.json")
                if tf.exists():
                    tf.unlink(missing_ok=True)
            except Exception as e:
                logger.error(f"[Guide] _clear_tests.unlink_timeline: {e}")

            self._set_footer("Guia limpo. Pronto para um novo experimento.")
            logger.info("[Guide] Limpeza concluída.")
        except Exception as e:
            logger.error(f"[Guide] _clear_tests erro: {e}")
            try:
                QMessageBox.warning(self, "Limpar", f"Falha ao limpar: {e}")
            except Exception:
                pass

    def _mark_all_done(self):
        """Marca todos os passos como concluídos, atualiza timeline e progresso."""
        try:
            logger.info("[Guide] Marcando todos os passos como concluídos...")
            for card in self.cards:
                try:
                    card.set_done(True)
                    st = card.step
                    sid = st.get("id") or f"step_{id(card)}"
                    self.timeline.setdefault(sid, {})
                    if "start" not in self.timeline[sid]:
                        self._mark_timeline(st, "start")
                    self._mark_timeline(st, "end")
                except Exception as e:
                    logger.error(f"[Guide] _mark_all_done.card: {e}")

            try:
                self._write_timeline()
            except Exception as e:
                logger.error(f"[Guide] _mark_all_done.persist: {e}")

            self._refresh_timeline_bar()
            self._set_footer("Todos os passos marcados como concluídos.")
            logger.info("[Guide] Todos os passos concluídos (forçado).")
        except Exception as e:
            logger.error(f"[Guide] _mark_all_done erro: {e}")
            try:
                QMessageBox.warning(self, "Marcar ✓", f"Falha: {e}")
            except Exception:
                pass

    def _apply_window_flags(self):
        flags = (Qt.Window | Qt.WindowTitleHint | Qt.WindowSystemMenuHint
                 | Qt.WindowMinimizeButtonHint | Qt.WindowMaximizeButtonHint | Qt.WindowCloseButtonHint)
        self.setWindowFlags(flags)
        try:
            screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
            geo = (screen.availableGeometry() if screen else None)
            if geo:
                self.setGeometry(geo)
        except Exception as e:
            logger.error(f"[Guide] posicionamento: {e}")

    def _load_theme(self):
        try:
            base = Path(__file__).resolve().parent
            candidates = [
                base.parent / "futuristic.qss",
            ]
            for p in candidates:
                try:
                    if p.exists():
                        qss = p.read_text(encoding="utf-8")
                        self.setStyleSheet(qss)
                        logger.info(f"[Guide] tema carregado: {p}")
                        return
                except Exception:
                    continue
            logger.error(f"[Guide] tema não encontrado. Procurados: {', '.join(str(p) for p in candidates)}")
        except Exception as e:
            logger.error(f"[Guide] tema: {e}")

    # ---------- Watchdog / Loading ----------
    def _start_loading_with_watchdog(self):
        self._load_steps_async()
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(self._on_loading_slow)
        self._watchdog.start(5000)

        self._watchdog2 = QTimer(self)
        self._watchdog2.setSingleShot(True)
        self._watchdog2.timeout.connect(self._on_loading_very_slow)
        self._watchdog2.start(20000)

        self._set_footer("Carregando passos do guia (parser oficial)…")

    def _load_steps_async(self):
        def job():
            return parse_yaml_to_steps(self.yaml_path or "", self.ssh)
        w = _FnWorker(job)
        self._loader_worker = w
        self._keep_worker(w)
        w.result.connect(self._on_loader_ok)
        w.error.connect(self._on_loader_err)
        w.finished.connect(lambda: self._cleanup_worker(w))
        w.start()

    def _on_loading_slow(self):
        if self._rendered_real or self._rendered_fallback:
            return
        try:
            if self._loading_spinner:
                self._loading_spinner.base = "Carregando (parser oficial demorando…)"
            self._loading_label.setText("Carregando passos… (oficial pode demorar por rede/SSH)")
            self._set_footer("Aguarde: obtendo passos oficiais…")
        except Exception as e:
            logger.error(f"[Guide] slow: {e}")

    def _on_loading_very_slow(self):
        if self._rendered_real or self._rendered_fallback:
            return
        try:
            steps = naive_yaml_quick_parse(self.yaml_path)
            self._render_steps(steps, replace=True)
            self._rendered_fallback = True
            self._ignore_loader_results = False
            self._set_footer("Guia básico exibido — oficial substituirá ao concluir.")
        except Exception as e:
            logger.error(f"[Guide] very_slow falhou: {e}")

    # ---------- Loader result ----------
    def _on_loader_ok(self, steps: list[dict]):
        if self._ignore_loader_results:
            return
        if (not self._official_mode) and self._only_yaml_actions:
            try:
                steps = self._filter_only_yaml_steps(steps)
            except Exception as e:
                logger.error(f"[Guide] filtro only_yaml: {e}")
        self._render_steps(steps, replace=True)
        self._rendered_real = True
        self._set_footer("Passos prontos (parser oficial).")
        self.guide_loaded.emit(len(steps))

        # Sincroniza ícones dos cards com o último snapshot que o MainWindow já possa ter enviado
        try:
            if self._last_states:
                self.reflect_status_map(self._last_states)
        except Exception as e:
            logger.error(f"[Guide] pós-load reflect: {e}")

    def _on_loader_err(self, msg: str):
        logger.error(f"[Guide] loader erro: {msg}")
        if not self._rendered_fallback:
            self._render_steps([{
                "id": "fallback_error", "title": "Falha ao carregar YAML",
                "description": msg, "command": "", "host": "attacker", "tags": ["erro"], "eta": "", "artifacts": []
            }], replace=True)
        self._set_footer("Falha ao carregar o guia.")
        self.guide_failed.emit(msg)

    # ---------- Render / Cards ----------
    def _render_steps(self, steps: list[dict], replace: bool):
        try:
            if self._watchdog: self._watchdog.stop()
            if self._watchdog2: self._watchdog2.stop()
            if self._loading_spinner: self._loading_spinner.stop("")
            if self._loading_label:
                try:
                    self.cards_layout.removeWidget(self._loading_label)
                    self._loading_label.deleteLater()
                except Exception:
                    pass
        except Exception:
            pass

        if replace:
            while self.cards_layout.count():
                item = self.cards_layout.takeAt(0)
                w = item.widget()
                if w:
                    w.setParent(None)
                    w.deleteLater()
            self.cards.clear()

        for i, st in enumerate(steps, start=1):
            card = StepCard(i, st)
            card.run_clicked.connect(self._run_step_async)
            card.ssh_clicked.connect(lambda host, cmd, c=card: self._ssh_exec_or_paste(host, cmd, c))
            card.mark_done.connect(lambda s: self._mark_timeline(s, "done"))
            self.cards_layout.addWidget(card)
            self.cards.append(card)

        spacer = QWidget(); spacer.setFixedHeight(6)
        self.cards_layout.addWidget(spacer)
        self._refresh_timeline_bar()

        # Ao renderizar, se já há snapshot de status, refletir imediatamente
        try:
            if self._last_states:
                self.reflect_status_map(self._last_states)
        except Exception as e:
            logger.error(f"[Guide] render reflect: {e}")

    def _update_yaml_header_label(self):
        try:
            if self.yaml_path and Path(self.yaml_path).exists():
                p = Path(self.yaml_path)
                self.lbl_yaml.setText(f"YAML: {p.name}")
                self.lbl_yaml.setToolTip(str(p))
            else:
                self.lbl_yaml.setText("YAML: (oficial)")
                self.lbl_yaml.setToolTip("Parser oficial (sem arquivo selecionado).")
        except Exception as e:
            logger.error(f"[Guide] yaml label: {e}")

    # ---------- Botões topo ----------
    def _reload_official(self):
        self._ignore_loader_results = False
        self._rendered_real = False
        self._official_mode = True
        self._only_yaml_actions = False
        self.lbl_yaml.setText("YAML: (oficial)")
        self._set_footer("Recarregando (parser oficial)…")
        self._load_steps_async()

    def _on_pick_yaml_in_guide(self):
        try:
            start_dir = str((Path(self.yaml_path).parent if self.yaml_path else (self.project_root / "lab" / "experiments")))
        except Exception:
            start_dir = "."
        path, _ = QFileDialog.getOpenFileName(self, "Escolher YAML de experimento (Guia)", start_dir, "YAML (*.yaml *.yml)")
        if not path:
            return
        p = Path(path)
        if not p.exists():
            QMessageBox.warning(self, "YAML", f"Arquivo não existe:\n{path}")
            return
        self.yaml_path = str(p)
        self._official_mode = False
        self._only_yaml_actions = True
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
            logger.error(f"[Guide] sync main: {e}")
        self._update_yaml_header_label()
        self._load_steps_async()

    def _filter_only_yaml_steps(self, steps: list[dict]) -> list[dict]:
        OFFICIAL_IDS = {
            "preflight", "up_vms",
            "attacker_prepare", "sensor_prepare",
            "attacker_tools_check", "sensor_tools_check",
            "connectivity", "sensor_capture_show",
            "hydra_lists"
        }
        out = []
        for st in steps:
            sid = (st.get("id") or "").strip()
            tags = [str(t).lower() for t in (st.get("tags") or [])]
            if sid in OFFICIAL_IDS:
                continue
            if any(t in ("infra", "diagnostic", "safety") for t in tags):
                continue
            out.append(st)
        return out or steps

    # ---------- Console / ações inferiores ----------
    def _save_console_to_file(self):
        try:
            default = str((self.project_root / ".meta" / f"{Path(self.yaml_path).stem}_console.log").resolve())
            Path(default).parent.mkdir(parents=True, exist_ok=True)
            path, _ = QFileDialog.getSaveFileName(self, "Salvar log do console", default, "Log (*.log);;Texto (*.txt)")
            if not path:
                return
            Path(path).write_text(self.console.toPlainText(), encoding="utf-8")
            self._set_footer(f"Log salvo em: {path}")
        except Exception as e:
            logger.error(f"[Guide] salvar console: {e}")
            QMessageBox.warning(self, "Salvar log", f"Falha: {e}")

    def _toggle_isolation_async(self):
        def job():
            from lab.security.safety import toggle_attacker_nat
            return toggle_attacker_nat(self.ssh, enable=False)
        def ok(_):
            self._set_footer("Atacante isolado. (Use o botão novamente no app principal para remover)")
            self._append_console("[guide] Isolamento aplicado.")
        def fail(msg: str):
            self._append_console(f"[erro] Isolamento: {msg}")
            QMessageBox.critical(self, "Isolamento", f"Falha: {msg}")
        w = _FnWorker(job)
        self._keep_worker(w)
        w.result.connect(ok)
        w.error.connect(fail)
        w.finished.connect(lambda: self._cleanup_worker(w))
        w.start()

    def _run_runner_async(self):
        self._append_console("[guide] Iniciando Runner com o YAML atual…")
        def job():
            try:
                from app.core import runner as core_runner
                if hasattr(core_runner, "run_from_yaml"):
                    return core_runner.run_from_yaml(self.yaml_path)
                if hasattr(core_runner, "main"):
                    return core_runner.main(["--yaml", self.yaml_path])
            except Exception as e:
                logger.error(f"[Guide] import runner: {e}")
            proc = subprocess.run([sys.executable, "-m", "app.core.runner", "--yaml", self.yaml_path],
                                  capture_output=True, text=True)
            return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
        def ok(res):
            if isinstance(res, dict):
                out, err, rc = res.get("stdout", ""), res.get("stderr", ""), res.get("returncode", 0)
                if out: self._append_console(out)
                if err: self._append_console(err)
                self._append_console(f"[guide] Runner finalizado (rc={rc}).")
            else:
                self._append_console("[guide] Runner finalizado.")
            self._set_footer("Dataset gerado (veja a pasta data/).")
        def fail(msg: str):
            self._append_console(f"[erro] Runner: {msg}")
            QMessageBox.critical(self, "Runner", f"Falha: {msg}")
        w = _FnWorker(job)
        self._keep_worker(w)
        w.result.connect(ok)
        w.error.connect(fail)
        w.finished.connect(lambda: self._cleanup_worker(w))
        w.start()

    # ---------- Execução de passos ----------
    def _run_all_steps(self):
        if self._stream_worker and self._stream_worker.isRunning():
            self._append_console("[warn] Há um passo em execução. Cancelando antes do lote…")
            self._cancel_running(wait_worker=True)
        if not self.cards:
            QMessageBox.warning(self, "Rodar todos", "Não há passos para executar.")
            return

        queue = []
        for c in self.cards:
            st = dict(c.step)
            cmd = (st.get("command") or "").strip()
            if not cmd:
                cmd = (st.get("command_normal") or st.get("command_b64") or "").strip()
                if cmd:
                    st["command"] = cmd
            if st.get("command"):
                queue.append(st)

        if not queue:
            QMessageBox.information(self, "Rodar todos", "Nenhum passo executável encontrado.")
            return

        self._batch_running = True
        self._batch_queue = queue
        self._append_console(f"[guide] Rodando {len(queue)} passo(s) em sequência…")
        self._set_footer("Execução em lote iniciada…")
        self.batch_started.emit(len(queue))
        QTimer.singleShot(120, lambda: self._run_step_async(self._batch_queue.pop(0)))

    def _run_step_async(self, step: dict):
        host = step.get("host", "attacker")
        cmd = (step.get("command", "") or "").strip()
        if not cmd:
            QMessageBox.warning(self, "Sem comando", "Este passo não definiu um comando executável.")
            return

        try:
            if any(tok in cmd for tok in ("{attacker_ip}", "{victim_ip}", "{sensor_ip}")):
                from app.core.yaml_parser import resolve_guest_ips, substitute_vars
                ips = resolve_guest_ips(self.ssh)
                cmd = substitute_vars(cmd, ips)
        except Exception as e:
            logger.error(f"[Guide] placeholders: {e}")

        if self._stream_worker and self._stream_worker.isRunning():
            self._append_console("[guide] Encerrando stream anterior…")
            self._cancel_running(wait_worker=True)

        card = self._find_card(step)
        if card: card.set_running()
        self._mark_timeline(step, "start")
        self._append_console("")
        self._append_console(f"=== PASSO: {step.get('title', '(sem título)')} | host={host} ===")
        self._append_console(f"$ {cmd}")

        timeout = int(step.get("timeout", 600))
        w = _StreamWorker(self.ssh, host, cmd, timeout_s=timeout)
        self._stream_worker = w
        self._keep_worker(w)
        self._current_card = card
        self._current_worker = w
        self._cancel_requested = False

        def _is_current(worker) -> bool:
            try:
                return (worker is self._stream_worker) and (worker is self._current_worker)
            except Exception:
                return False

        def _on_worker_done_guard(worker=w, c=card, s=step):
            if not _is_current(worker):
                logger.warning("[Guide] Sinal 'done' de worker antigo ignorado.")
                return
            if self._cancel_requested:
                try:
                    if c:
                        c.set_cancelled()
                    self._append_console("[guide] Passo cancelado pelo usuário.")
                except Exception as e:
                    logger.error(f"[Guide] marcar cancelado (done_guard): {e}")
                return
            self._on_step_done(c, s, ok=True)

        def _on_worker_error_guard(msg: str, worker=w, c=card, s=step):
            if not _is_current(worker):
                logger.warning("[Guide] Sinal 'error' de worker antigo ignorado.")
                return
            self._on_step_fail(c, s, msg)

        def _on_worker_finished_guard(worker=w, c=card, s=step):
            if not _is_current(worker):
                logger.warning("[Guide] Sinal 'finished' de worker antigo ignorado.")
                return
            try:
                self._on_step_final(c, s)
            finally:
                self._cleanup_worker(worker)
                self._current_card = None
                self._current_worker = None
                self._cancel_requested = False
                try:
                    if hasattr(self, "_update_footer_actions_enabled"):
                        self._update_footer_actions_enabled()
                except Exception as e:
                    logger.warning(f"[Guide] footer actions update: {e}")

        w.line.connect(self._append_console)
        w.finished_ok.connect(_on_worker_done_guard)
        w.error.connect(_on_worker_error_guard)
        w.finished.connect(_on_worker_finished_guard)
        # w.line.connect(self._append_console)
        # w.finished_ok.connect(lambda: self._on_step_done(card, step, ok=True))
        # w.error.connect(lambda msg: self._on_step_fail(card, step, msg))
        # w.finished.connect(lambda: (self._on_step_final(card, step), self._cleanup_worker(w)))
        self._set_footer(f"Executando passo em {host}…")
        w.start()

    def _on_step_done(self, card: StepCard | None, step: dict, ok: bool):
        if card:
            card.set_done(ok)
        self._append_console("[guide] Passo concluído.")

    def _on_step_fail(self, card: StepCard | None, step: dict, msg: str):
        if card:
            card.set_error()
        self._append_console(f"[erro] {msg}")
        try:
            QMessageBox.critical(self, "Erro no passo", msg)
        except Exception:
            pass

    def _on_step_final(self, card: StepCard | None, step: dict):
        # Não sobrepor 'done'/'error' para 'idle'; preserve estado p/ timeline.
        try:
            if card is not None:
                state = str(card.status.property("state") or "")
                if state not in ("done", "error", "cancelled"):
                    card.set_idle()
        except Exception as e:
            logger.error(f"[Guide] _on_step_final.state: {e}")

        self._mark_timeline(step, "end")
        self._write_timeline()
        self._refresh_timeline_bar()

        if self._batch_running:
            if self._batch_queue:
                self._append_console("[guide] Próximo passo em 0.15s…")
                QTimer.singleShot(150, lambda: self._run_step_async(self._batch_queue.pop(0)))
            else:
                self._batch_running = False
                self._append_console("[guide] Lote concluído ✓")
                self._set_footer("Execução em lote concluída.")
                self.batch_finished.emit()

    def _cancel_running(self, wait_worker: bool = False):
        try:
            self._cancel_requested = True
            if self._current_card:
                try:
                    self._current_card.set_cancelled()
                except Exception as e:
                    logger.warning(f"[Guide] Falha ao marcar cartão como cancelado: {e}")

            if hasattr(self.ssh, "cancel_all_running"):
                self.ssh.cancel_all_running()
        except Exception as e:
            logger.error(f"[Guide] cancel_all_running: {e}")

        try:
            if self._stream_worker and self._stream_worker.isRunning():
                self._stream_worker.stop()
                if wait_worker:
                    self._stream_worker.wait(2000)
        except Exception:
            pass

        self._append_console("[guide] Cancelamento solicitado.")
        self._set_footer("Cancelamento solicitado.")
        self._batch_running = False
        self._batch_queue = []

        try:
            if hasattr(self, "_update_footer_actions_enabled"):
                self._update_footer_actions_enabled()
        except Exception as e:
            logger.warning(f"[Guide] footer actions update: {e}")

    # ---------- SSH helpers ----------
    def _ssh_exec_or_paste(self, host: str, cmd: str, card: StepCard):
        self._append_console(f"[guide] Preparando SSH para '{host}'…")

        def job():
            parent = self.parent()
            if parent is not None and hasattr(parent, "_ssh_paste"):
                parent._ssh_paste(host, cmd or "")
                return f"ssh_paste:{host}"

            st = self.vagrant.status_by_name(host)
            if st != "running":
                return {"warn": f"{host} não está 'running' (rode: vagrant up {host})."}

            self.vagrant.wait_ssh_ready(host, str(self.lab_dir), attempts=10, delay_s=3)
            self.ssh.open_external_terminal(host)

            try:
                session = f"guide_{host}"
                self.ssh.run_command(host, f"tmux new-session -d -s {session} || true", timeout=20)
                payload = (cmd or "").strip()
                if payload:
                    import shlex
                    quoted = shlex.quote(payload.replace("\r\n", "\n"))
                    self.ssh.run_command(host, f"tmux send-keys -t {session} {quoted} C-m", timeout=20)
                return f"fallback_tmux:{host}"
            except Exception as e:
                return {"warn": f"SSH aberto (sem tmux) — envie manualmente. Detalhe: {e}"}

        def ok(res):
            if isinstance(res, dict) and "warn" in res:
                self._append_console(f"[warn] {res['warn']}")
                card.set_ssh_done(res["warn"])
                self._set_footer(res["warn"])
            else:
                self._append_console(f"[guide] SSH ativo em {host}. Comando (se fornecido) foi enviado.")
                card.set_ssh_done("Comando enviado via SSH ✓")
                self._set_footer(f"SSH ativo em {host}.")

        def fail(msg: str):
            card.set_ssh_done("Falha ao enviar comando via SSH ✖")
            self._append_console(f"[erro] SSH: {msg}")
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

    # ---------- Linha do tempo ----------
    def _mark_timeline(self, step, phase):
        sid = step.get("id") or f"step_{int(time.time())}"
        self.timeline.setdefault(sid, {})
        self.timeline[sid][phase] = time.time()

    def _write_timeline(self):
        try:
            meta_dir = self.project_root / ".meta"
            meta_dir.mkdir(parents=True, exist_ok=True)
            f = meta_dir / (Path(self.yaml_path).stem + "_timeline.json")
            f.write_text(json.dumps(self.timeline, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"[Guide] persist timeline: {e}")

    def _refresh_timeline_bar(self):
        total = len(self.cards)
        if total <= 0:
            self.timeline_bar.setValue(0)
            return
        done = 0
        for c in self.cards:
            st = c.status.property("state") or "idle"
            if st == "done":
                done += 1
        pct = int((done / total) * 100)
        self.timeline_bar.setValue(max(0, min(100, pct)))

    # ---------- Util ----------
    def _find_card(self, step: dict) -> StepCard | None:
        for c in self.cards:
            if c.step is step or (c.step.get("id") and c.step.get("id") == step.get("id")):
                return c
        return None

    def _append_console(self, text: str):
        try:
            self.console.appendPlainText(text)
        except Exception as e:
            logger.error(f"[Guide] console: {e}")

    def _set_footer(self, text: str):
        try:
            self.lbl_footer.setText(text)
        except Exception:
            pass

    # ---------- Workers lifecycle ----------
    def _keep_worker(self, w: QThread):
        try:
            self._workers.add(w)
            w.finished.connect(lambda: self._workers.discard(w))
        except Exception as e:
            logger.error(f"[Guide] keep worker: {e}")

    def _cleanup_worker(self, w: QThread):
        try:
            self._workers.discard(w)
        except Exception:
            pass

    def reject(self):
        try:
            if self._workers:
                self._cancel_running()
        except Exception:
            pass
        super().reject()

    def reflect_machine_status(self, name: str, state: str):
        """
        Recebe atualizações da tela principal (MainWindow/_apply_status_to_cards).
        Ex.: name='attacker', state='running' -> ícones 'online' nos passos de host attacker.
        """
        try:
            role = role_from_host(name)
            vis = "online" if state == "running" else "offline"
            logger.info(f"[Guide] reflect_machine_status: {name} -> {state} (role={role}, vis={vis})")
            for c in self.cards:
                if c.matches_role(role) or c.matches_host(name):
                    c.set_machine_visibility(vis)

            self._update_footer_actions_enabled()
        except Exception as e:
            logger.error(f"[Guide] reflect_machine_status: {e}")

    def reflect_status_map(self, states: Dict[str, str]):
        """
        Atualiza o snapshot completo (dict {name: state}) e reflete nos cards.
        Chame isso de dentro de MainWindow._apply_status_to_cards.
        """
        try:
            self._last_states.update(states or {})
            for name, st in (states or {}).items():
                self.reflect_machine_status(name, st)
            self._update_footer_actions_enabled()
        except Exception as e:
            logger.error(f"[Guide] reflect_status_map: {e}")
