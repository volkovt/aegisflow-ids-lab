"""
UI futurista (tema Matrix) para o laboratório de IDS/ML do TCC.

Arquitetura:
- ActionDockWidget (esquerda): ações agrupadas (Infra, Dataset/Experimentos).
- MachinesBoard (centro): cards de máquinas com desenho de "computador" e ações rápidas.
- LogConsole (base): QPlainTextEdit para logs, com fonte monoespaçada e realce neon.
- MatrixRainWidget (fundo): animação leve de "digital rain" (mouse-transparent).

Observação:
- O main tenta importar componentes desacoplados de app.ui2.*.
- Se ainda não existirem, define fallbacks internos equivalentes (rodável agora).
- Mantém compatibilidade com o core atual (Vagrant/SSH/DatasetController, etc.).
"""

import inspect
import os
import platform
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtGui import QCursor, Qt
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QPlainTextEdit, QGroupBox, QMessageBox, QFileDialog, QProgressBar,
    QMainWindow, QFrame, QSizePolicy, QScrollArea, QLayout, QToolButton, QSplitter,
)
from PySide6.QtCore import QTimer, Signal, QThread, QEvent, QSettings

from app.core.data_collector import WarmupCoordinator
from app.core.default_presets import preset_all, preset_scan_brute, preset_dos, preset_brute_http, preset_heavy_syn
from app.core.logger_setup import setup_logger
from app.core.config_loader import load_config
from app.core.pathing import get_project_root, find_config
from app.core.preflight import run_preflight
from app.core.preflight_enforcer import PreflightEnforcer
from app.core.vagrant_manager import VagrantManager
from app.core.ssh_manager import SSHManager

import warnings

from app.core.workers.result_worker import ResultWorker
from app.core.workers.worker import Worker
from app.ui.components.action_dock import ActionDockWidgetExt
from app.ui.components.machine_avatar import MachineAvatarExt
from app.ui.components.machine_card import MachineCardWidgetExt
from app.ui.components.ui_runner_shim import UiRunnerShim
from app.ui.components.spinner_animation import _SpinnerAnimator
from app.ui.step_card import ExperimentGuideDialog
from app.ui.yaml_designer import YAMLDesignerDialog

try:
    from cryptography.utils import CryptographyDeprecationWarning
    warnings.filterwarnings("ignore", category=CryptographyDeprecationWarning)
except Exception:
    pass

LOG_DIR = Path(".logs")

def _self_contained(fn):
    def inner(*args, **kwargs):
        logger = setup_logger(LOG_DIR)
        logger.info(f"[UI] Iniciando {fn.__name__}...")
        try:
            return fn(*args, **kwargs)
        finally:
            logger.info(f"[UI] Finalizado {fn.__name__}.")
    return inner

class MainWindow(QMainWindow):
    log_line = Signal(str)

    def __init__(self):
        super().__init__()
        self.first_show = True
        self._guide_dialog = None
        self._ssh_tmux_sessions = {}
        self._workers = set()
        self._workers_lock = threading.RLock()
        self.warmup = WarmupCoordinator(warmup_window_s=30)
        self.log_line.connect(self._append_log_gui)

        self.setWindowTitle("VagrantLabUI — ML IDS Lab (Matrix Edition)")
        self.logger = setup_logger(LOG_DIR)

        self.project_root = get_project_root(Path(__file__))
        try:
            self.cfg_path = find_config(self.project_root / "config.yaml")
            self.cfg = load_config(self.cfg_path)
            self.machine_by_name = {m.name: m for m in self.cfg.machines}
        except Exception as e:
            QMessageBox.critical(self, "Erro de Config", str(e))
            raise

        self.project_root = Path.cwd()
        self.lab_dir = self.project_root / self.cfg.lab_dir

        self.vagrant = VagrantManager(self.project_root, self.lab_dir)
        self.ssh = SSHManager(self.lab_dir)
        self.preflight = PreflightEnforcer(self.vagrant, self.lab_dir)

        try:
            from app.core.dataset_controller import DatasetController
        except Exception:
            raise ImportError("Módulos do DatasetController não encontrados em app/core/dataset_controller.py")
        self._ds_shim = UiRunnerShim(self.ssh, self.lab_dir, self.project_root, self.preflight, self._append_log)
        self._ds_controller = DatasetController(self._ds_shim)

        self._ds_controller.started.connect(self._on_ds_started)
        self._ds_controller.finished.connect(self._on_ds_finished)

        self.current_yaml_path = (self.project_root / "lab" / "experiments" / "exp_all.yaml")
        self._yaml_selected_by_user = False
        self._ensure_experiment_presets()

        self._build_ui()
        self._load_theme()

    def _build_ui(self):
        ActionDock = ActionDockWidgetExt
        MachineCard = MachineCardWidgetExt
        MatrixRain = MachineAvatarExt

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.settings = QSettings("VagrantLabUI", "MatrixEdition")

        content = QWidget()
        content.setObjectName("matrixRoot")
        content_l = QVBoxLayout(content)
        content_l.setContentsMargins(0, 0, 0, 0)
        content_l.setSpacing(0)
        root.addWidget(content, 1)

        top = QHBoxLayout()
        top.setContentsMargins(12, 8, 12, 8)
        lbl = QLabel("◤ MATRIX OPS")
        lbl.setObjectName("matrixBrand")
        top.addWidget(lbl)
        top.addStretch(1)
        content_l.addLayout(top)

        body = QHBoxLayout()
        body.setContentsMargins(12, 0, 12, 0)
        body.setSpacing(12)
        content_l.addLayout(body, 1)

        self.dock = ActionDock()
        self.dock.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.dock.setMinimumWidth(265)

        self.status_bar = self.dock.status_bar
        self.btn_write = self.dock.btn_write
        self.btn_up_all = self.dock.btn_up_all
        self.btn_status = self.dock.btn_status
        self.btn_halt_all = self.dock.btn_halt_all
        self.btn_destroy_all = self.dock.btn_destroy_all
        self.btn_preflight = self.dock.btn_preflight

        self.btn_yaml_designer = self.dock.btn_yaml_designer
        self.btn_pick_yaml = self.dock.btn_pick_yaml
        self.btn_generate_dataset = self.dock.btn_generate_dataset
        self.btn_open_guide = self.dock.btn_open_guide
        self.btn_open_data = self.dock.btn_open_data

        try:
            for b in (
                    self.btn_write, self.btn_up_all, self.btn_status,
                    self.btn_halt_all, self.btn_destroy_all, self.btn_preflight,
                    self.btn_yaml_designer, self.btn_pick_yaml, self.btn_generate_dataset,
                    self.btn_open_guide, self.btn_open_data
            ):
                b.setSizePolicy(QSizePolicy.Expanding, b.sizePolicy().verticalPolicy())
        except Exception as e:
            self._append_log(f"[UI] ajuste dock/buttons: {e}")


        board_wrap = QFrame()
        board_wrap.setObjectName("machinesBoard")
        board_l = QVBoxLayout(board_wrap)
        board_l.setAlignment(Qt.AlignTop)
        board_l.setContentsMargins(12, 12, 12, 12)
        board_l.setSpacing(12)

        gb = QGroupBox("Máquinas do Lab")
        gb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        wrap = QWidget()
        wrap_l = QVBoxLayout(wrap)
        wrap_l.setSizeConstraint(QLayout.SetNoConstraint)
        wrap_l.setAlignment(Qt.AlignTop)
        wrap_l.setContentsMargins(0, 0, 0, 0)
        wrap_l.setSpacing(8)

        self.cards = {}
        for m in self.cfg.machines:
            card = MachineCard(m.name)
            self.cards[m.name] = card
            card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            wrap_l.addWidget(card)

            card.installEventFilter(self)

            card.act_up.triggered.connect(lambda _=False, n=m.name, b=card.menu_btn: self._on_up_vm(n, b))
            card.act_status.triggered.connect(lambda _=False, n=m.name, b=card.menu_btn: self._run_status_by_name(n, b))
            card.act_restart.triggered.connect(lambda _=False, n=m.name, b=card.menu_btn: self._on_restart_vm(n, b))
            card.act_halt.triggered.connect(
                lambda _=False, n=m.name, b=card.menu_btn: self._run_vagrant(self.vagrant.halt, n, b, "Halt…", "Ações"))
            card.act_destroy.triggered.connect(lambda _=False, n=m.name, b=card.menu_btn: self._on_destroy_vm(n, b))
            card.act_ssh.triggered.connect(lambda _=False, n=m.name, b=card.menu_btn: self._ssh(n, b))

        machinesScroll = QScrollArea()
        machinesScroll.setObjectName("machinesScroll")
        machinesScroll.setWidgetResizable(True)
        machinesScroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        machinesScroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        machinesScroll.setFrameShape(QFrame.NoFrame)
        machinesScroll.setAlignment(Qt.AlignTop)
        machinesScroll.setWidget(wrap)

        self.machinesScroll = machinesScroll
        self.machinesWrap = wrap

        wrap.setMinimumSize(0, 0)
        wrap.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.MinimumExpanding)
        self.machinesScroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        gb_layout = QVBoxLayout(gb)
        gb_layout.setAlignment(Qt.AlignTop)
        gb_layout.setContentsMargins(8, 8, 8, 8)
        gb_layout.setSpacing(6)
        gb_layout.addWidget(machinesScroll)

        board_l.addWidget(gb, 1)

        self.global_progress = QProgressBar()
        self.global_progress.setRange(0, 0)
        self.global_progress.setVisible(False)
        self.global_progress.setObjectName("globalProgress")
        board_l.addWidget(self.global_progress)

        console = QGroupBox("Console")
        cv = QVBoxLayout(console)
        cv.setAlignment(Qt.AlignTop)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setObjectName("logConsole")
        self.log_view.setMinimumHeight(240)
        console.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        cv.addWidget(self.log_view)

        board_l.addWidget(console, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setObjectName("boardScroll")
        scroll.setWidget(board_wrap)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.splitter = QSplitter(Qt.Horizontal, self)
        self.splitter.setObjectName("mainSplitter")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.dock)
        self.splitter.addWidget(scroll)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setHandleWidth(6)
        self.splitter.setSizes([265, 1000])

        body.addWidget(self.splitter, 1)

        try:
            sizes = self.settings.value("splitter/sizes")
            if isinstance(sizes, (list, tuple)) and len(sizes) == 2:
                self.splitter.setSizes([int(sizes[0]), int(sizes[1])])
        except Exception as e:
            self._append_log(f"[UI] Falha ao restaurar splitter: {e}")

        try:
            self.splitter.splitterMoved.connect(
                lambda *_: self._append_log(f"[UI] Splitter movido: {self.splitter.sizes()}")
            )
        except Exception as e:
            self._append_log(f"[UI] Falha ao conectar splitterMoved: {e}")

        self._rain = MatrixRain(self)
        self._rain.setObjectName("matrixRain")
        self._rain.lower()
        self._pos_rain_timer = QTimer(self)
        self._pos_rain_timer.setSingleShot(True)

        self.btn_write.clicked.connect(self.on_write)
        self.btn_up_all.clicked.connect(self.on_click_up_all)
        self.btn_status.clicked.connect(self.on_status)
        self.btn_halt_all.clicked.connect(self.on_halt_all)
        self.btn_destroy_all.clicked.connect(self.on_destroy_all)
        self.btn_preflight.clicked.connect(self.on_preflight)

        self.btn_yaml_designer.clicked.connect(self.on_yaml_designer)
        self.btn_pick_yaml.clicked.connect(self.on_pick_yaml)
        self.btn_generate_dataset.clicked.connect(self.on_generate_dataset)
        self.btn_open_guide.clicked.connect(self.on_open_guide)
        self.btn_open_data.clicked.connect(lambda: self._open_folder(self.project_root / "data"))

        self.updateGeometry()

    def showEvent(self, event):
        try:
            if self.first_show:
                self.first_show = False

                try:
                    screen = QApplication.screenAt(QCursor.pos())
                    geo = screen.availableGeometry()
                    self.setGeometry(geo)
                    self.showMaximized()
                except Exception:
                    screen = QApplication.primaryScreen()
                    if screen is not None:
                        geo = screen.availableGeometry()
                        self.setGeometry(geo)

        except Exception as e:
            self._append_log(f"[UI] showEvent: {e}")

    def _on_ds_started(self):
        try:
            self._append_log("[Dataset] Iniciando…")
            self.global_progress.setVisible(True)
            self.btn_generate_dataset.setText("Cancelar")
            self._ds_spinner = getattr(self, "_ds_spinner", None)
            if self._ds_spinner is None:
                self._ds_spinner = _SpinnerAnimator(self.btn_generate_dataset, "Gerando dataset…")
            self._ds_spinner.start()
        except Exception as e:
            self._append_log(f"[WARN] start dataset ui: {e}")

    def _on_ds_finished(self, status: str):
        try:
            self.global_progress.setVisible(False)
            if hasattr(self, "_ds_spinner") and self._ds_spinner:
                self._ds_spinner.stop("Gerar Dataset (YAML)")
            self.btn_generate_dataset.setText("Gerar Dataset (YAML)")
            self._append_log(f"[Dataset] Finalizado com status: {status}")
        except Exception as e:
            self._append_log(f"[WARN] finish dataset ui: {e}")

    def _set_busy(self, busy: bool, msg: str = ""):
        try:
            for b in [self.btn_write, self.btn_up_all, self.btn_status,
                      self.btn_halt_all, self.btn_destroy_all, self.btn_preflight,
                      self.btn_yaml_designer, self.btn_pick_yaml, self.btn_generate_dataset,
                      self.btn_open_data]:
                b.setEnabled(not busy)
            if busy:
                self.status_bar.setText(msg or "Aguarde...")
                QApplication.setOverrideCursor(Qt.WaitCursor)
            else:
                self.status_bar.setText("")
                QApplication.restoreOverrideCursor()
        except Exception as e:
            self._append_log(f"[WARN] _set_busy: {e}")

    def _keep_worker(self, w, tag=""):
        try:
            with self._workers_lock:
                self._workers.add(w)
            self._on_worker_start(tag)
            try:
                w.done.connect(lambda: self._on_worker_done(tag, w))
            except Exception:
                pass
            self._append_log(f"[Thread] iniciado {tag or w}")
        except Exception as e:
            self._append_log(f"[WARN] _keep_worker: {e}")

    def _on_worker_start(self, tag: str):
        try:
            self.global_progress.setVisible(True)
            self._status_spinner = getattr(self, "_status_spinner", None)
            msg = f"{tag or 'tarefa'} em execução…"
            if self._status_spinner is None:
                self._status_spinner = _SpinnerAnimator(self.status_bar, msg)
            else:
                self._status_spinner.base_text = msg
            self._status_spinner.start()
        except Exception as e:
            self._append_log(f"[WARN] _on_worker_start: {e}")

    def _on_worker_done(self, tag: str, w):
        try:
            with self._workers_lock:
                self._workers.discard(w)
            if not self._workers:
                self.global_progress.setVisible(False)
                if hasattr(self, "_status_spinner") and self._status_spinner:
                    self._status_spinner.stop("")
        except Exception as e:
            self._append_log(f"[WARN] _on_worker_done: {e}")

    def _wire_button_with_worker(self, btn, worker, active_label: str, idle_label: str):
        try:
            target = btn if not isinstance(btn, QToolButton) else self.status_bar
            spinner = _SpinnerAnimator(target, active_label)
            spinner.start()

            def _restore():
                try:
                    spinner.stop("" if target is self.status_bar else idle_label)
                    if target is self.status_bar and hasattr(btn, "setText"):
                        btn.setText(idle_label)
                except Exception as e:
                    self._append_log(f"[WARN] restore botão: {e}")

            worker.done.connect(_restore)
            worker.error.connect(lambda msg: _restore())
        except Exception as e:
            self._append_log(f"[WARN] _wire_button_with_worker: {e}")

    def on_write(self):
        try:
            def job():
                from jinja2 import Environment, FileSystemLoader
                env = Environment(loader=FileSystemLoader(str(self.project_root / "app" / "templates")))
                vf = self.vagrant.write_vagrantfile(self.project_root / "app" / "templates", self.cfg.to_template_ctx())
                return str(vf)

            w = ResultWorker(job)
            self._wire_button_with_worker(self.btn_write, w, "Gerando…", "Gerar Vagrantfile")
            w.result.connect(lambda p: self._append_log(f"Vagrantfile gerado em: {p}"))
            w.error.connect(lambda msg: self._append_log(f"[ERRO] Gerar Vagrantfile: {msg}"))
            self._keep_worker(w, tag="write_vagrantfile")
            w.start()

        except Exception as e:
            self._append_log(f"Erro ao gerar Vagrantfile: {e}")

    def on_click_up_all(self):
        def gen():
            names = ["attacker", "sensor", "victim"]

            template_dir = Path(self.project_root) / "app" / "templates"
            ctx = self._build_vagrant_ctx_from_yaml(self.current_yaml_path)

            yield "[UpAll] Sincronizando Vagrantfile…"
            try:
                vf_path, vf_hash, changed = self.vagrant.ensure_vagrantfile_synced(template_dir, ctx)
                status = "atualizado" if changed else "inalterado"
                yield f"[UpAll] Vagrantfile {status}: {vf_path.name} (hash {vf_hash[:8]})."
            except Exception as e:
                yield f"[UpAll] Falha ao sincronizar Vagrantfile: {e}"

            yield "[UpAll] Iniciando criação/subida das VMs…"
            try:
                _ = self.vagrant.status()
                states = {n: self.vagrant.status_by_name(n) for n in names}
            except Exception as e:
                yield f"[Status] Falha ao consultar: {e}"
                states = {}

            to_create = [n for n in names if states.get(n) in (None, "not_created", "pre_transient")]
            to_start = [n for n in names if n not in to_create]

            for n in to_create + to_start:
                yield f"[UpAll] Garantindo {n}…"
                try:
                    for ln in self.vagrant.ensure_created_and_running(
                            n, template_dir, ctx, attempts=20, delay_s=4
                    ):
                        yield ln
                    self.warmup.mark_boot(n)
                    yield f"[Warmup] {n}: janela de aquecimento iniciada (30s)."
                except Exception as e:
                    yield f"[UpAll] Falha em {n}: {e}"

            yield "[UpAll] Concluído."
            return "ok"

        try:
            worker = Worker(gen)
            worker.line.connect(self._append_log)

            def finalize():
                try:
                    out = self.vagrant.status()
                    self._apply_status_to_cards(out)
                    self._append_log("[UpAll] Finalizado.")
                except Exception as e:
                    self._append_log(f"[UpAll] Falha ao atualizar status: {e}")

            worker.done.connect(finalize)
            worker.error.connect(lambda msg: self._append_log(f"[ERRO] Up All: {msg}"))

            self._wire_button_with_worker(self.btn_up_all, worker, "Subindo VMs…", "Subir todas")
            self._keep_worker(worker, tag="up_all")
            worker.start()
        except Exception as e:
            self._append_log(f"[ERRO] Up All: {e}")

    def on_status(self):
        try:
            self.btn_status.setEnabled(False)
            self._append_log("[Status] Atualizando…")

            buffer = []

            worker = Worker(self.vagrant.status_stream)
            worker.line.connect(lambda ln: buffer.append(ln))
            worker.error.connect(lambda msg: self._append_log(f"[ERRO] {msg}"))

            def finalize():
                out = "\n".join(buffer)
                self._append_log("Status geral:\n" + out)
                self._apply_status_to_cards(out)
                self.btn_status.setEnabled(True)

            worker.done.connect(finalize)
            self._wire_button_with_worker(self.btn_status, worker, "Status…", "Status")
            self._keep_worker(worker, tag="status_stream")
            worker.start()
        except Exception as e:
            self._append_log(f"Erro no status: {e}")
            self.btn_status.setEnabled(True)

    def status_by_name(self, name: str):
        try:
            self._append_log(f"[Status] Checando {name}…")
            w = ResultWorker(self.vagrant.status_by_name, name)

            def on_result(out):
                self._append_log(f"Status de {name}: {out}")
                card = self.cards[name]
                if out == "running":
                    card.statusDot.setProperty("status", "running")
                elif out == "poweroff":
                    card.statusDot.setProperty("status", "stopped")
                else:
                    card.statusDot.setProperty("status", "unknown")
                card.statusDot.style().unpolish(card.statusDot)
                card.statusDot.style().polish(card.statusDot)
                self._spawn_info_update(name, out)

            w.result.connect(on_result)
            w.error.connect(lambda msg: self._append_log(f"Erro no status de {name}: {msg}"))
            self._keep_worker(w, tag=f"status_by_name:{name}")
            w.start()
        except Exception as e:
            self._append_log(f"Erro no status de {name}: {e}")

    def _on_restart_vm(self, name: str, btn: QPushButton | None = None):
        def gen():
            try:
                self._append_log(f"[Restart] Reiniciando {name}…")
                try:
                    self._append_log("[Thread] Parando threads antes de 'reload'…")
                    self._quiesce_background(reason="reload", timeout_s=6)
                except Exception as e:
                    self._append_log(f"[WARN] reload quiesce: {e}")

                try:
                    st = self.vagrant.status_by_name(name)
                except Exception as e:
                    st = None
                    yield f"[Restart] Falha ao consultar status de {name}: {e}"

                if st != "running":
                    yield f"[Restart] {name} não está 'running' — executando Up."
                    try:
                        for ln in self.vagrant.up(name):
                            yield ln
                    except Exception as e:
                        yield f"[ERRO] Up {name}: {e}"
                        return "error"
                else:
                    try:
                        if hasattr(self.vagrant, "reload"):
                            yield f"[Restart] Executando vagrant reload em {name}…"
                            for ln in self.vagrant.reload(name):
                                yield ln
                        else:
                            yield f"[Restart] reload indisponível; executando Halt + Up em {name}…"
                            for ln in self.vagrant.halt(name):
                                yield ln
                            for ln in self.vagrant.up(name):
                                yield ln
                    except Exception as e:
                        yield f"[ERRO] Restart {name}: {e}"
                        return "error"

                try:
                    self.vagrant.wait_ssh_ready(name, str(self.lab_dir), attempts=10, delay_s=3)
                    yield f"[Restart] {name} está 'running' e SSH pronto."
                except Exception as e:
                    yield f"[Restart] {name} 'running' porém SSH ainda não respondeu: {e}"

                try:
                    self.warmup.mark_boot(name)
                    yield f"[Warmup] {name}: janela de aquecimento iniciada (30s)."
                except Exception as e:
                    yield f"[Warmup] Falha ao marcar boot de {name}: {e}"

                yield "[Restart] Concluído."
                return "ok"

            except Exception as e:
                yield f"[ERRO] Restart {name}: {e}"
                return "error"

        try:
            worker = Worker(gen)
            worker.line.connect(self._append_log)
            worker.error.connect(lambda msg: self._append_log(f"[ERRO] Restart {name}: {msg}"))

            if btn is not None:
                self._wire_button_with_worker(btn, worker, "Restart…", "Restart")

            worker.done.connect(lambda: self.status_by_name(name))
            self._keep_worker(worker, tag=f"restart:{name}")
            worker.start()
        except Exception as e:
            self._append_log(f"[ERRO] _on_restart_vm({name}): {e}")

    def on_halt_all(self):
        self._run_vagrant(self.vagrant.halt, btn=self.btn_halt_all, active_label="Halt…", idle_label="Halt todas")

    def on_destroy_all(self):
        confirm = QMessageBox.question(self, "Confirmar", "Destruir TODAS as VMs? Esta ação é irreversível.")
        if confirm == QMessageBox.Yes:
            self._run_vagrant(self.vagrant.destroy, btn=self.btn_destroy_all, active_label="Destroy…",
                              idle_label="Destroy todas")

    def on_preflight(self):
        try:
            worker = Worker(run_preflight, self.project_root, self.lab_dir, self.cfg, self.vagrant, self.ssh)
            worker.line.connect(self._append_log)
            worker.error.connect(lambda msg: self._append_log(f"[ERRO] {msg}"))
            worker.done.connect(lambda: self._append_log("[OK] Preflight finalizado. Relatório em .logs/lab_preflight.txt"))
            self._wire_button_with_worker(self.btn_preflight, worker, "Preflight…", "Preflight")
            self._keep_worker(worker, tag="preflight")
            worker.start()
        except Exception as e:
            self._append_log(f"Erro ao iniciar preflight: {e}")

    def on_pick_yaml(self):
        try:
            path, _ = QFileDialog.getOpenFileName(
                self, "Escolher YAML de experimento",
                str(self.current_yaml_path if self.current_yaml_path.exists() else (
                            self.project_root / "lab" / "experiments")),
                "YAML (*.yaml *.yml)"
            )
            if path:
                self.current_yaml_path = Path(path)
                self._yaml_selected_by_user = True
                self._append_log(f"[Dataset] YAML selecionado: {self.current_yaml_path}")
            else:
                self._append_log("[Dataset] YAML não alterado.")
        except Exception as e:
            self._append_log(f"[ERRO] Escolher YAML: {e}")

    def on_generate_dataset(self):
        try:
            w = getattr(self._ds_controller, "_worker", None)
            if w and w.is_alive():
                self._append_log("[Dataset] Cancelamento solicitado pelo usuário.")
                try:
                    self._ds_controller.cancel()
                except Exception as e:
                    self._append_log(f"[ERRO] Cancel: {e}")
                return

            yaml_path = str(self.current_yaml_path)
            out_dir = str(self.project_root / "data")
            self._ds_controller.start(yaml_path, out_dir)

        except Exception as e:
            self._append_log(f"[ERRO] Dataset: {e}")

    # ========= Utilidades e compat =========
    def _open_folder(self, path: Path):
        try:
            path = Path(path)
            path.mkdir(parents=True, exist_ok=True)
            if platform.system() == "Windows":
                os.startfile(str(path))
            elif platform.system() == "Darwin":
                subprocess.check_call(["open", str(path)])
            else:
                subprocess.check_call(["xdg-open", str(path)])
            self._append_log(f"[UI] Abrindo pasta: {path}")
        except Exception as e:
            self._append_log(f"[UI] Falha ao abrir pasta: {e}")

    def _infer_os_from_box(self, box: str) -> str:
        try:
            b = (box or "").lower()
            if "kali" in b:
                return "Kali Linux"
            if "ubuntu" in b:
                return "Ubuntu"
            if "debian" in b:
                return "Debian"
            if "centos" in b or "rocky" in b or "almalinux" in b:
                return "RHEL-like"
            if "windows" in b or "win" in b:
                return "Windows"
            return box or "desconhecido"
        except Exception as e:
            self._append_log(f"[WARN] _infer_os_from_box: {e}")
            return "desconhecido"

    def _collect_machine_details(self, name: str, state_hint: str | None = None) -> tuple[str, str, str]:
        try:
            m = self.machine_by_name[name]
        except KeyError:
            self._append_log(f"[WARN] VM '{name}' não encontrada no config.")
            return ("desconhecido", "—", "—")

        guest_ip = f"{self.cfg.ip_base}{m.ip_last_octet}"
        os_text = self._infer_os_from_box(m.box)

        try:
            state = state_hint if state_hint is not None else self.vagrant.status_by_name(name)
            if state == "running":
                try:
                    self.vagrant.wait_ssh_ready(name, str(self.lab_dir), attempts=10, delay_s=3)
                except Exception as e:
                    self._append_log(f"[WARN] wait_ssh_ready falhou em {name}: {e}")
                try:
                    f = self.ssh.get_ssh_fields_safe(name)
                    host_endpoint = f"{f.get('HostName', '?')}:{f.get('Port', '?')}"
                except Exception as e:
                    self._append_log(f"[WARN] ssh-config falhou em {name}: {e}")
                    host_endpoint = "—"

                try:
                    os_text = self._query_os_friendly(name, timeout=12)
                except Exception as e:
                    self._append_log(f"[WARN] _query_os_friendly falhou em {name}: {e}")
                    try:
                        cmd_os = 'uname -sr || ( [ -r /etc/os-release ] && . /etc/os-release && printf "%s\n" "$PRETTY_NAME" )'
                        out = self.ssh.run_command(name, cmd_os, timeout=10).strip()
                        if out:
                            os_text = out.splitlines()[0].strip()
                    except Exception as e2:
                        self._append_log(f"[WARN] uname/os-release falhou em {name}: {e2}")
                        try:
                            outw = self.ssh.run_command(
                                name,
                                'powershell -NoProfile -Command "(Get-CimInstance Win32_OperatingSystem).Caption"',
                                timeout=10
                            ).strip()
                            if outw:
                                os_text = outw
                        except Exception as ew:
                            self._append_log(f"[WARN] PowerShell OS falhou em {name}: {ew}")

                return (os_text, host_endpoint, guest_ip)
            else:
                return (os_text, "—", guest_ip)

        except Exception as e:
            self._append_log(f"[WARN] _collect_machine_details erro em {name}: {e}")
            return (os_text, "—", guest_ip)

    def _set_card_info(self, name: str, os_text: str, host_endpoint: str, guest_ip: str):
        try:
            card = self.cards[name]
            if hasattr(card, "_set_card_info"):
                card._set_card_info(os_text, host_endpoint, guest_ip)
                return
            if hasattr(card, "set_pill_values"):
                card.set_pill_values(os_text, host_endpoint, guest_ip)
                return
            if hasattr(card, "pills"):
                card.pills["so"].setValue(os_text)
                card.pills["host"].setValue(host_endpoint)
                card.pills["guest"].setValue(guest_ip)
            self._append_log(f"[UI] _set_card_info aplicado para {name}.")
        except Exception as e:
            self._append_log(f"[WARN] _set_card_info falhou para {name}: {e}")

    def _update_machine_info(self, name: str):
        try:
            os_text, host_endpoint, guest_ip = self._collect_machine_details(name)
            self._set_card_info(name, os_text, host_endpoint, guest_ip)
        except Exception as e:
            self._append_log(f"[WARN] _update_machine_info erro em {name}: {e}")

    def _run_status_by_name(self, name: str, btn: QPushButton):
        try:
            self._append_log(f"[Status] Checando {name}…")
            w = ResultWorker(self.vagrant.status_by_name, name)

            self._wire_button_with_worker(btn, w, "Status…", "Status")

            def on_result(out):
                self._append_log(f"Status de {name}: {out}")
                card = self.cards[name]
                if out == "running":
                    card.statusDot.setProperty("status", "running")
                elif out == "poweroff":
                    card.statusDot.setProperty("status", "stopped")
                else:
                    card.statusDot.setProperty("status", "unknown")
                card.statusDot.style().unpolish(card.statusDot)
                card.statusDot.style().polish(card.statusDot)
                self._spawn_info_update(name, out)

            w.result.connect(on_result)
            w.error.connect(lambda msg: self._append_log(f"Erro no status de {name}: {msg}"))
            self._keep_worker(w, tag=f"status_by_name:{name}")
            w.start()
        except Exception as e:
            self._append_log(f"Erro no status de {name}: {e}")

    def _apply_status_to_cards(self, out: str):
        try:
            states = {}
            for line in out.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2:
                    name = parts[0]
                    st = parts[1]
                    states[name] = st

            for name, card in self.cards.items():
                st = states.get(name, "unknown")
                if st == "running":
                    card.statusDot.setProperty("status", "running")
                elif st in ("poweroff", "powered", "aborted"):
                    card.statusDot.setProperty("status", "stopped")
                else:
                    card.statusDot.setProperty("status", "unknown")
                card.statusDot.style().unpolish(card.statusDot)
                card.statusDot.style().polish(card.statusDot)

            running_names = [n for n, st in states.items() if st == "running"]
            for i, n in enumerate(running_names):
                delay_ms = 1200 * i
                QTimer.singleShot(delay_ms, lambda name=n, st="running": self._spawn_info_update(name, st))

        except Exception as e:
            self._append_log(f"[WARN] _apply_status_to_cards: {e}")

    def _spawn_info_update(self, name: str, state: str):
        def job():
            return name, *self._collect_machine_details(name, state_hint=state)

        w = ResultWorker(job)
        w.result.connect(lambda res: self._set_card_info(*res))
        w.error.connect(lambda msg: self._append_log(f"[WARN] Info {name} falhou: {msg}"))
        self._keep_worker(w, tag=f"info:{name}")
        w.start()

    def _build_vagrant_ctx_from_yaml(self, yaml_path: Path | None) -> dict:
        try:
            base_ctx = self.cfg.to_template_ctx()
        except Exception as e:
            self._append_log(f"[Vagrant] Falha ao obter ctx do config: {e}")
            raise

        ctx = dict(base_ctx)
        try:
            machines = [dict(m) for m in (base_ctx.get("machines") or [])]
        except Exception:
            machines = []

        if not machines:
            try:
                machines = []
                for m in self.cfg.machines:
                    machines.append({
                        "name": m.name,
                        "box": m.box,
                        "ip_last_octet": m.ip_last_octet,
                        "memory": getattr(m, "memory", None),
                        "cpus": getattr(m, "cpus", None),
                    })
            except Exception as e:
                self._append_log(f"[Vagrant] Falha ao normalizar máquinas: {e}")

        victim_ip = None
        yaml_p = yaml_path or getattr(self, "current_yaml_path", None)
        if yaml_p:
            try:
                from app.core.yaml_parser import _safe_load_yaml
                data = _safe_load_yaml(str(yaml_p)) or {}
                victim_ip = ((data.get("targets") or {}).get("victim_ip") or "").strip() or None
            except Exception as e:
                self._append_log(f"[Guide] falha ao carregar YAML: {e}")

        try:
            if victim_ip:
                ip_base = getattr(self.cfg, "ip_base", ctx.get("ip_base"))
                if ip_base and victim_ip.startswith(ip_base):
                    try:
                        last_octet = int(victim_ip.split(".")[-1])
                    except Exception:
                        last_octet = None

                    if last_octet is not None:
                        for m in machines:
                            if (m.get("name") or "").lower() == "victim":
                                old = m.get("ip_last_octet")
                                m["ip_last_octet"] = last_octet
                                self._append_log(
                                    f"[Vagrant] Ajustando victim ip_last_octet {old}→{last_octet} (do YAML).")
                                break
                    else:
                        self._append_log(f"[WARN] victim_ip inválido no YAML: {victim_ip}")
                else:
                    self._append_log(
                        f"[WARN] victim_ip do YAML ({victim_ip}) não corresponde ao ip_base do lab "
                        f"({getattr(self.cfg, 'ip_base', 'desconhecido')}). Mantendo config."
                    )
        except Exception as e:
            self._append_log(f"[WARN] Não foi possível ajustar IP da vítima: {e}")

        ctx["machines"] = machines
        ctx["ip_base"] = getattr(self.cfg, "ip_base", ctx.get("ip_base"))
        return ctx

    def _query_os_friendly(self, name: str, timeout: int = 12) -> str:
        cmd_linux = r"""
            set -e
            get_name() {
              if command -v lsb_release >/dev/null 2>&1; then
                lsb_release -ds && printf " (%s)\n" "$(lsb_release -cs)"
                return
              fi
              if [ -r /etc/os-release ]; then
                . /etc/os-release
                printf "%s\n" "${PRETTY_NAME:-$NAME $VERSION}"
                return
              fi
              if [ -r /etc/redhat-release ]; then
                cat /etc/redhat-release
                return
              fi
              if [ -r /etc/debian_version ]; then
                printf "Debian %s\n" "$(cat /etc/debian_version)"
                return
              fi
              uname -sr
            }
            NAME="$(get_name || true)"
            ARCH="$(uname -m || echo '?')"
            KERN="$(uname -r || echo '?')"
            NAME="${NAME#\"}"; NAME="${NAME%\"}"
            printf "%s (%s, kernel %s)\n" "$NAME" "$ARCH" "$KERN"
            """.strip()
        try:
            out = self.ssh.run_command(name, cmd_linux, timeout=timeout).strip()
            if out:
                self._append_log(f"[SO] {name}: {out}")
                return out
        except Exception as e:
            self._append_log(f"[WARN] coleta SO (Linux) falhou em {name}: {e}")

        ps = (
            r'powershell -NoProfile -Command '
            r'"$o=Get-CimInstance Win32_OperatingSystem; '
            r'Write-Output ($o.Caption + " " + $o.Version + " (" + $o.OSArchitecture + ", build " + $o.BuildNumber + ")")"'
        )
        try:
            outw = self.ssh.run_command(name, ps, timeout=timeout).strip()
            if outw:
                self._append_log(f"[SO] {name}: {outw}")
                return outw
        except Exception as e:
            self._append_log(f"[WARN] coleta SO (Windows) falhou em {name}: {e}")

        try:
            out2 = self.ssh.run_command(name, "uname -sr", timeout=8).strip()
            if out2:
                self._append_log(f"[SO] {name} (fallback): {out2}")
                return out2
        except Exception as e:
            self._append_log(f"[WARN] coleta SO fallback (uname) falhou em {name}: {e}")
        return "SO desconhecido"

    def _ensure_experiment_presets(self):
        try:
            exp_dir = self.project_root / "lab" / "experiments"
            exp_dir.mkdir(parents=True, exist_ok=True)

            def write_if_missing(path: Path, content: str):
                if not path.exists():
                    path.write_text(content, encoding="utf-8")
                    self._append_log(f"[Dataset] Preset criado: {path}")

            exp_all = exp_dir / "exp_all.yaml"
            write_if_missing(exp_all, preset_all())

            exp_scan_brute = exp_dir / "exp_scan_brute.yaml"
            write_if_missing(exp_scan_brute, preset_scan_brute())

            exp_dos = exp_dir / "exp_dos.yaml"
            write_if_missing(exp_dos, preset_dos())

            exp_brute_http = exp_dir / "exp_brute_http.yaml"
            write_if_missing(exp_brute_http, preset_brute_http())

            exp_heavy_syn = exp_dir / "exp_heavy_syn.yaml"
            write_if_missing(exp_heavy_syn, preset_heavy_syn())
        except Exception as e:
            self._append_log(f"[WARN] Falha ao garantir presets: {e}")

    def on_yaml_designer(self):
        try:
            dlg = YAMLDesignerDialog(
                parent=self,
                initial_path=self.current_yaml_path if self.current_yaml_path.exists() else None,
                experiments_dir=self.project_root / "lab" / "experiments"
            )
            dlg.exec()
            if dlg.current_path:
                self.current_yaml_path = dlg.current_path
                self._append_log(f"[Dataset] YAML atual: {self.current_yaml_path}")
        except Exception as e:
            self._append_log(f"[UI] Erro ao abrir Designer (YAML): {e}")

    def _mark_boot_if_running(self, name: str) -> None:
        try:
            st = self.vagrant.status_by_name(name)
            if st == "running":
                try:
                    self.warmup.mark_boot(name)
                    self._append_log(f"[Warmup] {name}: janela de aquecimento iniciada (30s).")
                except Exception as e:
                    self._append_log(f"[Warmup] Falha ao marcar boot de {name}: {e}")
            else:
                self._append_log(f"[Warmup] {name} não está 'running' (estado: {st}).")
        except Exception as e:
            self._append_log(f"[Warmup] Falha ao checar status de {name}: {e}")

    def _up_vm_and_mark(self, name: str) -> None:
        try:
            self._append_log(f"[Up] Subindo {name}...")
            self.vagrant.up(name)
        except Exception as e:
            self._append_log(f"[Up] Falha ao subir {name}: {e}")
            return

        self._mark_boot_if_running(name)

        try:
            self.vagrant.wait_ssh_ready(name, str(self.lab_dir), attempts=10, delay_s=3)
            self._append_log(f"[Up] {name} está 'running' e SSH pronto.")
        except Exception as e:
            self._append_log(f"[Up] {name} 'running' porém SSH ainda não respondeu: {e}")

    def on_open_guide(self):
        try:
            yaml_for_guide = str(
                self.current_yaml_path) if self._yaml_selected_by_user and self.current_yaml_path else ""
            dlg = ExperimentGuideDialog(
                yaml_path=yaml_for_guide,
                ssh=self.ssh,
                vagrant=self.vagrant,
                lab_dir=str(self.lab_dir),
                project_root=str(self.project_root),
            )
            dlg.show()
            self._guide_dialog = dlg
        except Exception as e:
            self._append_log(f"[ERRO] Abrir Guia: {e}")
            QMessageBox.critical(self, "Guia do Experimento", str(e))

    def _reset_busy_ui(self):
        try:
            self.global_progress.setVisible(False)
            if hasattr(self, "_status_spinner") and self._status_spinner:
                try:
                    self._status_spinner.stop("")
                except Exception as e:
                    self._append_log(f"[WARN] _reset_busy_ui spinner: {e}")
            self.status_bar.setText("")
            QApplication.restoreOverrideCursor()
        except Exception as e:
            self._append_log(f"[WARN] _reset_busy_ui: {e}")

    def _cancel_worker(self, w, reason: str = ""):
        try:
            if hasattr(w, "request_cancel"):
                w.request_cancel(reason)
            if hasattr(w, "cancel"):
                w.cancel()
            if hasattr(w, "requestInterruption"):
                try:
                    w.requestInterruption()
                except Exception:
                    pass
            if hasattr(w, "quit"):
                try:
                    w.quit()
                except Exception:
                    pass
        except Exception as e:
            self._append_log(f"[WARN] _cancel_worker: {e}")

    def _quiesce_background(self, reason: str = "quiesce", timeout_s: int = 5):
        try:
            self._append_log(f"[Thread] Quiescendo background por '{reason}'…")
            with self._workers_lock:
                workers = list(self._workers)

            for w in workers:
                try:
                    self._cancel_worker(w, reason)
                except Exception as e:
                    self._append_log(f"[WARN] cancel worker: {e}")

            deadline = time.time() + max(1, timeout_s)
            for w in workers:
                try:
                    remaining = max(0.0, deadline - time.time())
                    if remaining > 0:
                        w.wait(int(remaining * 1000))
                except Exception as e:
                    self._append_log(f"[WARN] wait worker: {e}")

            with self._workers_lock:
                still_running = [x for x in self._workers if getattr(x, "isRunning", lambda: False)()]
            for w in still_running:
                try:
                    self._append_log("[Thread] Forçando término de worker remanescente…")
                    w.terminate()
                except Exception as e:
                    self._append_log(f"[WARN] terminate worker: {e}")

            self._reset_busy_ui()
        except Exception as e:
            self._append_log(f"[WARN] _quiesce_background: {e}")

    def _load_theme(self):
        try:
            qss = (Path(__file__).parent / "futuristic.qss").read_text(encoding="utf-8")
            self.setStyleSheet(qss)
        except Exception as e:
            self.logger.warning(f"[UI] Falha ao carregar tema: {e}")

    # ========= SSH/Terminal (mantidos) =========
    def _ssh(self, name: str, btn: QPushButton | None = None):
        def gen():
            try:
                yield f"[SSH] Preparando {name}…"
                st = self.vagrant.status_by_name(name)
                if st != "running":
                    yield f"[WARN] {name} não está 'running' (rode: vagrant up {name})."
                    return "skip"

                try:
                    self.vagrant.wait_ssh_ready(name, str(self.lab_dir), attempts=10, delay_s=3)
                    yield f"[SSH] {name} com SSH pronto."
                except Exception as e:
                    yield f"[WARN] SSH ainda não pronto em {name}: {e}"
                    return "skip"

                yield f"Abrindo terminal SSH externo para {name}…"
                try:
                    self.ssh.open_external_terminal(name)
                    return "ok"
                except Exception as e:
                    yield f"[ERRO] Falha ao abrir SSH externo: {e}"
                    return "error"
            except Exception as e:
                yield f"[ERRO] SSH {name}: {e}"
                return "error"

        try:
            worker = Worker(gen)
            worker.line.connect(self._append_log)
            worker.error.connect(lambda msg: self._append_log(f"[ERRO] SSH {name}: {msg}"))

            if btn is not None:
                self._wire_button_with_worker(btn, worker, "SSH", "SSH")

            self._keep_worker(worker, tag=f"ssh:{name}")
            worker.start()
        except Exception as e:
            self._append_log(f"[ERRO] _ssh: {e}")

    # ========= Logging thread-safe =========
    def _append_log(self, text: str):
        try:
            frame = inspect.currentframe().f_back
            lineno = frame.f_lineno if frame else -1
        except Exception:
            lineno = -1

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted = f"{timestamp} | linha {lineno} | {text}"

        try:
            self.logger.info(text)
        except Exception:
            pass

        try:
            if QThread.currentThread() is QApplication.instance().thread():
                self._append_log_gui(formatted)
            else:
                self.log_line.emit(formatted)
        except Exception:
            pass

    def _append_log_gui(self, formatted: str):
        try:
            self.log_view.appendPlainText(formatted)
        except Exception as e:
            import logging
            logging.error(f"Falha ao atualizar log_view: {e}")


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
