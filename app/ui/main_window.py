from __future__ import annotations
from pathlib import Path
import logging
import inspect
from datetime import datetime

from PySide6.QtCore import Qt, Signal, QThread, QTimer, QSettings
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGroupBox, QPlainTextEdit,
    QMainWindow, QFrame, QSizePolicy, QScrollArea, QLayout, QMessageBox, QFileDialog, QProgressBar, QToolButton,
)

from app.core.logger_setup import setup_logger
from app.core.config_loader import load_config
from app.core.pathing import get_project_root, find_config
from app.core.preflight_enforcer import PreflightEnforcer
from app.core.vagrant_manager import VagrantManager
from app.core.ssh_manager import SSHManager
from app.core.preflight import run_preflight
from app.core.data_collector import WarmupCoordinator
from app.core.workers.os_worker import refresh_os_async

from app.ui.components.action_dock import ActionDockWidgetExt
from app.ui.components.machine_card import MachineCardWidgetExt
from app.ui.components.machine_avatar import MachineAvatarExt
from app.ui.components.ui_runner_shim import UiRunnerShim
from app.ui.guide.guide_dialog import ExperimentGuideDialog
from app.ui.yaml_designer import YAMLDesignerDialog

from app.ui.services.task_manager import TaskManager
from app.ui.services.machine_info_service import MachineInfoService
from app.ui.services.vagrant_ctx_service import VagrantContextService
from app.ui.services.preset_service import PresetBootstrapper
from app.ui.services.theme_loader import load_theme
from app.ui.controllers.main_controller import MainController


LOG_DIR = Path(".logs")


class MainWindow(QMainWindow):
    log_line = Signal(str)
    osTextArrived = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.first_show = True
        self._guide_dialog = None

        self.logger = setup_logger(LOG_DIR)
        self.project_root = get_project_root(Path(__file__))
        self.cfg_path = find_config(self.project_root / "config.yaml")
        self.cfg = load_config(self.cfg_path)
        self.machine_by_name = {m.name: m for m in self.cfg.machines}

        self._os_threads = {}
        try:
            self.osTextArrived.connect(self._set_machine_os_text)
        except Exception as e:
            self.logger.error(f"[UI] connect osTextArrived: {e}")

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
        self.ds_controller = DatasetController(self._ds_shim)

        self.warmup = WarmupCoordinator(warmup_window_s=30)
        self.log_line.connect(self._append_log_gui)

        # UI
        self._build_ui()
        load_theme(self, Path(__file__).parent / "futuristic.qss", logger=self.logger)

        # Services/Controller
        self.tm = TaskManager(
            global_progress=self.global_progress,
            status_target=self.status_bar,
            append_log=self._append_log,
            logger=self.logger,
        )
        self.machine_info = MachineInfoService(
            vagrant=self.vagrant,
            ssh=self.ssh,
            cfg=self.cfg,
            lab_dir=self.lab_dir,
            append_log=self._append_log,
            logger=self.logger,
        )
        self.vagrant_ctx = VagrantContextService(cfg=self.cfg, append_log=self._append_log, logger=self.logger)
        self.presets = PresetBootstrapper(project_root=self.project_root, append_log=self._append_log, logger=self.logger)
        self.presets.ensure()

        self.ctrl = MainController(
            project_root=self.project_root,
            lab_dir=self.lab_dir,
            cfg=self.cfg,
            vagrant=self.vagrant,
            ssh=self.ssh,
            preflight=self.preflight,
            warmup=self.warmup,
            ds_controller=self.ds_controller,
            task_manager=self.tm,
            machine_info_service=self.machine_info,
            vagrant_ctx_service=self.vagrant_ctx,
            set_card_info_cb=self._set_card_info,
            apply_status_to_cards_cb=self._apply_status_to_cards,
            append_log=self._append_log,
            logger=self.logger,
        )

        self._wire_actions()

    # ------------- UI building -------------
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

        # Header
        content = QWidget(); content.setObjectName("matrixRoot")
        content_l = QVBoxLayout(content)
        content_l.setContentsMargins(0, 0, 0, 0)
        content_l.setSpacing(0)
        root.addWidget(content, 1)
        top = QHBoxLayout(); top.setContentsMargins(12, 8, 12, 8)
        lbl = QLabel("◤ MATRIX OPS"); lbl.setObjectName("matrixBrand");
        top.addWidget(lbl)
        top.addStretch(1)
        content_l.addLayout(top)

        # Body split (dock | board)
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

        # Board
        board_wrap = QFrame(); board_wrap.setObjectName("machinesBoard")
        board_l = QVBoxLayout(board_wrap)
        board_l.setAlignment(Qt.AlignTop)
        board_l.setContentsMargins(12, 12, 12, 12)
        board_l.setSpacing(12)
        gb = QGroupBox("Máquinas do Lab")
        gb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        wrap = QWidget(); wrap_l = QVBoxLayout(wrap)
        wrap_l.setSizeConstraint(QLayout.SetNoConstraint)
        wrap_l.setAlignment(Qt.AlignTop)
        wrap_l.setContentsMargins(0, 0, 0, 0)
        wrap_l.setSpacing(8)
        self.cards: dict[str, MachineCard] = {}
        for m in self.cfg.machines:
            card = MachineCard(m.name)
            self.cards[m.name] = card
            card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            wrap_l.addWidget(card)

            card.act_up.triggered.connect(
                lambda _=False, n=m.name, b=card.menu_btn: self.ctrl.up_vm(n, btn=b)
            )
            card.act_status.triggered.connect(lambda _=False, n=m.name, c=card: self._on_card_status(n, c))
            card.act_restart.triggered.connect(lambda _=False, n=m.name, b=card.menu_btn: self.ctrl.restart_vm(n, btn=b))
            card.act_halt.triggered.connect(lambda _=False, n=m.name: self.ctrl._run_simple_vagrant(self.vagrant.halt, btn=self.btn_halt_all, active_label="Halt…", idle_label="Halt todas"))
            card.act_destroy.triggered.connect(lambda _=False, n=m.name, b=card.menu_btn: self.ctrl._run_simple_vagrant(self.vagrant.destroy, btn=self.btn_destroy_all, active_label="Destroy…", idle_label="Destroy todas"))
            card.act_ssh.triggered.connect(lambda _=False, n=m.name: self.ctrl.ssh_open(n))

        self._last_state_map: dict[str, str] = {}

        machinesScroll = QScrollArea()
        machinesScroll.setObjectName("machinesScroll")
        machinesScroll.setWidgetResizable(True)
        machinesScroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        machinesScroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        machinesScroll.setFrameShape(QFrame.NoFrame)
        machinesScroll.setAlignment(Qt.AlignTop)
        machinesScroll.setWidget(wrap)
        self.machinesScroll = machinesScroll; self.machinesWrap = wrap

        gb_layout = QVBoxLayout(gb); gb_layout.setAlignment(Qt.AlignTop); gb_layout.setContentsMargins(8, 8, 8, 8); gb_layout.setSpacing(6)
        gb_layout.addWidget(machinesScroll)
        board_l.addWidget(gb, 1)

        self.global_progress = QProgressBar();
        self.global_progress.setRange(0, 0);
        self.global_progress.setFixedHeight(8);
        self.global_progress.setVisible(False);
        self.global_progress.setObjectName("globalProgress")
        board_l.addWidget(self.global_progress)

        console = QGroupBox("Console")
        cv = QVBoxLayout(console); cv.setAlignment(Qt.AlignTop)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setObjectName("logConsole")
        self.log_view.setMinimumHeight(240)
        console.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed); cv.addWidget(self.log_view)
        board_l.addWidget(console, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setObjectName("boardScroll")
        scroll.setWidget(board_wrap);
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        from PySide6.QtWidgets import QSplitter
        self.splitter = QSplitter(Qt.Horizontal, self)
        self.splitter.setObjectName("mainSplitter")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.dock)
        self.splitter.addWidget(scroll)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([265, 1000])
        body.addWidget(self.splitter, 1)

        try:
            sizes = QSettings("VagrantLabUI", "MatrixEdition").value("splitter/sizes")
            if isinstance(sizes, (list, tuple)) and len(sizes) == 2:
                self.splitter.setSizes([int(sizes[0]), int(sizes[1])])
        except Exception as e:
            self._append_log(f"[UI] Falha ao restaurar splitter: {e}")

        self._rain = MatrixRain(self); self._rain.setObjectName("matrixRain"); self._rain.lower()

    def _reflect_to_guide(self, name: str, state: str):
        try:
            if self._guide_dialog and hasattr(self._guide_dialog, "reflect_machine_status"):
                self._guide_dialog.reflect_machine_status(name, state)
        except Exception as e:
            self._append_log(f"[UI] reflect_to_guide: {e}")

    def _on_card_status(self, name: str, card):
        try:
            def apply(st: str):
                try:
                    self._set_card_status(card, st)
                    self._reflect_to_guide(name, st)

                    if st == "running":
                        QTimer.singleShot(0, lambda n=name: self._start_os_probe(n))
                except Exception as e:
                    self._append_log(f"[UI] _on_card_status.apply: {e}")

            self.ctrl.status_by_name(name, on_card_status=apply)
        except Exception as e:
            self._append_log(f"[UI] _on_card_status: {e}")

    def _set_global_progress(self, visible: bool):
        try:
            self.global_progress.setVisible(visible)
        except Exception as e:
            self._append_log(f"[UI] progress toggle: {e}")

    def _wire_actions(self):
        self.btn_write.clicked.connect(lambda: self.ctrl.write_vagrantfile(btn=self.btn_write))
        self.btn_up_all.clicked.connect(lambda: self.ctrl.up_all(btn=self.btn_up_all))
        self.btn_status.clicked.connect(lambda: self.ctrl.status_all(btn=self.btn_status))
        self.btn_halt_all.clicked.connect(lambda: self.ctrl.halt_all(btn=self.btn_halt_all))
        self.btn_destroy_all.clicked.connect(lambda: self.ctrl.destroy_all(btn=self.btn_destroy_all, confirm_dialog=lambda t, m: QMessageBox.question(self, t, m) == QMessageBox.Yes))
        self.btn_preflight.clicked.connect(self._on_preflight)
        self.btn_yaml_designer.clicked.connect(self._on_yaml_designer)
        self.btn_pick_yaml.clicked.connect(self._on_pick_yaml)
        self.btn_generate_dataset.clicked.connect(lambda: self.ctrl.generate_dataset(toggle_cancel=lambda: None))
        self.btn_open_guide.clicked.connect(self._on_open_guide)
        self.btn_open_data.clicked.connect(lambda: self.ctrl.open_folder(self.project_root / "data"))

        try:
            self.ds_controller.started.connect(lambda: self._set_global_progress(True))
            self.ds_controller.progress.connect(self._append_log)
            self.ds_controller.finished.connect(lambda _st: self._set_global_progress(False))
        except Exception as e:
            self._append_log(f"[UI] wire dataset progress: {e}")

    def _on_os_thread_finished(self, vm_name: str, th):
        try:
            self._os_threads.pop(vm_name, None)
            try:
                th.deleteLater()
            except Exception:
                pass
        except Exception as e:
            self._append_log(f"[UI] _on_os_thread_finished: {e}")

    def _kickoff_initial_os_probes(self):
        try:
            names = list(self.cards.keys())

            if getattr(self, "_last_state_map", None):
                names = [n for n, st in self._last_state_map.items() if st == "running"] or names

            for i, n in enumerate(names):
                QTimer.singleShot(150 * i, lambda vm=n: self._start_os_probe(vm))

            self._append_log(f"[UI] OS probes inicial disparado para: {', '.join(names)}")
        except Exception as e:
            self._append_log(f"[UI] kickoff probes: {e}")

    def _start_os_probe(self, vm_name: str):
        """Inicia o probe de OS, guarda a thread e conecta finished -> _on_os_thread_finished."""
        try:
            th = refresh_os_async(self, vm_name)
            if not th:
                return
            self._os_threads[vm_name] = th
            try:
                th.finished.connect(lambda n=vm_name, t=th: self._on_os_thread_finished(n, t))
            except Exception:
                try:
                    th.done.connect(lambda n=vm_name, t=th: self._on_os_thread_finished(n, t))
                except Exception:
                    pass
        except Exception as e:
            self._append_log(f"[UI] _start_os_probe: {e}")

    # ------------- Window events -------------
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

                    QTimer.singleShot(550, self._kickoff_initial_os_probes)
                except Exception as e:
                    self._append_log(f"[UI] showEvent: {e}")
        except Exception as e:
            self._append_log(f"[UI] showEvent outer: {e}")

    def closeEvent(self, event):
        try:
            QSettings("VagrantLabUI", "MatrixEdition").setValue("splitter/sizes", self.splitter.sizes())
            self.ctrl.detach_ui_log_handlers()
        except Exception as e:
            self._append_log(f"[UI] closeEvent: {e}")
        super().closeEvent(event)

    # ------------- UI helpers -------------
    def _set_card_status(self, card: MachineCardWidgetExt, state: str):
        try:
            card.set_status(state)
        except Exception as e:
            self._append_log(f"[WARN] _set_card_status: {e}")

    def _apply_status_to_cards(self, out: str):
        try:
            states = {}
            for line in out.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2:
                    states[parts[0]] = parts[1]

            self._last_state_map = dict(states)

            for name, card in self.cards.items():
                self._set_card_status(card, states.get(name, "unknown"))

            running = [n for n, st in states.items() if st == "running"]
            for i, n in enumerate(running):
                QTimer.singleShot(1200 * i, lambda name=n, st="running": self.ctrl.spawn_info_update(name, st))
                QTimer.singleShot(200 * i, lambda vm=n: self._start_os_probe(vm))

            if self._guide_dialog and hasattr(self._guide_dialog, "reflect_status_map"):
                self._append_log("[UI] Atualizando status no Guia do Experimento.")
                self._guide_dialog.reflect_status_map(states)
        except Exception as e:
            self._append_log(f"[WARN] _apply_status_to_cards: {e}")

    def _push_status_to_guide(self, states: dict[str, str] | None = None):
        """Empurra um snapshot de status para o Guia (cache ou inferido)."""
        try:
            if not self._guide_dialog:
                return

            snap = states or (getattr(self, "_last_state_map", {}) or {})
            if not snap:
                try:
                    snap = {
                        name: ("running" if getattr(card, "_vis", "offline") == "online" else "stopped")
                        for name, card in self.cards.items()
                    }
                except Exception as e:
                    self._append_log(f"[UI] infer snapshot fail: {e}")
                    snap = {}

            if snap and hasattr(self._guide_dialog, "reflect_status_map"):
                self._guide_dialog.reflect_status_map(snap)
                self._append_log("[UI] Snapshot inicial de status enviado ao Guia.")
        except Exception as e:
            self._append_log(f"[UI] push_status_to_guide: {e}")


    def _set_card_info(self, name: str, os_text: str, host_endpoint: str, guest_ip: str):
        try:
            card = self.cards.get(name)
            if not card:
                return
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

    def _set_machine_os_text(self, vm_name: str, os_text: str):
        try:
            card = self.cards.get(vm_name)
            if not card:
                return

            if hasattr(card, "pills") and "so" in card.pills:
                pill = card.pills["so"]
                value = os_text or "—"
                pill.setValue(value)
                pill.setToolTip(value)
            elif hasattr(card, "set_pill_values"):
                host = card.pills["host"].toolTip() if hasattr(card, "pills") and "host" in card.pills else "—"
                guest = card.pills["guest"].toolTip() if hasattr(card, "pills") and "guest" in card.pills else "—"
                card.set_pill_values(os_text or "—", host, guest)

            self._append_log(f"[SO] {vm_name}: {os_text}")
        except Exception as e:
            self._append_log(f"[UI] _set_machine_os_text: {e}")


    # ------------- Actions -------------
    def _on_preflight(self):
        try:
            from app.core.workers.worker import Worker
            worker = Worker(run_preflight, self.project_root, self.lab_dir, self.cfg, self.vagrant, self.ssh)
            worker.line.connect(self._append_log)
            worker.error.connect(lambda msg: self._append_log(f"[ERRO] {msg}"))
            worker.done.connect(lambda: self._append_log("[OK] Preflight finalizado. Relatório em .logs/lab_preflight.txt"))
            self.tm.wire_button(self.btn_preflight, worker, active_label="Preflight…", idle_label="Preflight")
            self.tm.keep(worker, tag="preflight")
            worker.start()
        except Exception as e:
            self._append_log(f"Erro ao iniciar preflight: {e}")

    def _on_yaml_designer(self):
        try:
            dlg = YAMLDesignerDialog(
                parent=self,
                initial_path=self.ctrl.current_yaml_path if self.ctrl.current_yaml_path.exists() else None,
                experiments_dir=self.project_root / "lab" / "experiments",
            )
            dlg.exec()
            if dlg.current_path:
                self.ctrl.current_yaml_path = dlg.current_path
                self._append_log(f"[Dataset] YAML atual: {self.ctrl.current_yaml_path}")
        except Exception as e:
            self._append_log(f"[UI] Erro ao abrir Designer (YAML): {e}")

    def _on_pick_yaml(self):
        try:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Escolher YAML de experimento",
                str(self.ctrl.current_yaml_path if self.ctrl.current_yaml_path.exists() else (self.project_root / "lab" / "experiments")),
                "YAML (*.yaml *.yml)",
            )
            if path:
                self.ctrl.current_yaml_path = Path(path)
                self.ctrl.yaml_selected_by_user = True
                self._append_log(f"[Dataset] YAML selecionado: {self.ctrl.current_yaml_path}")
            else:
                self._append_log("[Dataset] YAML não alterado.")
        except Exception as e:
            self._append_log(f"[ERRO] Escolher YAML: {e}")

    def _on_open_guide(self):
        try:
            yaml_for_guide = str(
                self.ctrl.current_yaml_path) if self.ctrl.yaml_selected_by_user and self.ctrl.current_yaml_path else ""
            dlg = self.ctrl.open_guide(dialog_factory=lambda **kw: ExperimentGuideDialog(**kw),
                                       yaml_for_guide=yaml_for_guide)
            self._guide_dialog = dlg
            try:
                if dlg is not None:
                    dlg.setParent(None)
                    dlg.show()
                    dlg.raise_()
                    dlg.activateWindow()
                    from PySide6.QtCore import Qt
                    dlg.setWindowState((dlg.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
            except Exception as e:
                self._append_log(f"[UI] bring-to-front Guide falhou: {e}")

            self._push_status_to_guide()
            QTimer.singleShot(50, lambda: self.ctrl.status_all(btn=self.btn_status))
        except Exception as e:
            self._append_log(f"[ERRO] Abrir Guia: {e}")
            QMessageBox.critical(self, "Guia do Experimento", str(e))

    # ------------- Logging -------------
    def _append_log(self, text: str):
        try:
            frame = inspect.currentframe().f_back
            lineno = frame.f_lineno if frame else -1
        except Exception:
            lineno = -1
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted = f"{timestamp} | linha {lineno} | {text}"

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
            logging.getLogger("[UI]").error(f"Falha ao atualizar log_view: {e}")



def run_app():
    app = QApplication([])
    win = MainWindow(); win.setWindowTitle("VagrantLabUI — ML IDS Lab (Matrix Edition)"); win.show()
    app.exec()
