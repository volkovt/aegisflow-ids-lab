import inspect
import os
import platform
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtGui import QCursor, Qt
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QPlainTextEdit, QGroupBox, QGridLayout, QMessageBox, QFileDialog, QProgressBar, QMainWindow
)
from PySide6.QtCore import QTimer, Signal, QThread

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
from app.ui.flow_layout import FlowLayout
from app.ui.info_pills import InfoPill
from app.ui.spinner_animation import _SpinnerAnimator
from app.ui.step_card import ExperimentGuideDialog
from app.ui.yaml_designer import YAMLDesignerDialog

try:
    from cryptography.utils import CryptographyDeprecationWarning
    warnings.filterwarnings("ignore", category=CryptographyDeprecationWarning)
except Exception:
    pass

LOG_DIR = Path(".logs")

def _import_orchestrator():
    """
    Tenta importar orquestrador tanto em 'orchestrator/*' quanto 'app/orchestrator/*'.
    Retorna (load_experiment_from_yaml, ExperimentRunner).
    """
    try:
        from lab.orchestrator.runner import ExperimentRunner
        from lab.orchestrator.yaml_loader import load_experiment_from_yaml
        return load_experiment_from_yaml, ExperimentRunner
    except Exception as e:
        raise ImportError(
            "Módulos de orquestração não encontrados. "
            "Crie as pastas 'orchestrator/', 'agents/', 'actions/', 'capture/', 'datasets/' e 'experiments/' "
            "conforme sugerido (ou use 'app/orchestrator/*')."
        ) from e

class UiRunnerShim:
    """
    Adaptador leve para o DatasetController.
    - Expõe .ssh para permitir cancel_all_running().
    - Faz preflight e chama o Runner real.
    - Se o Runner suportar cancel_event, repassa; se não, segue sem (fallback).
    """
    def __init__(self, ssh_manager, lab_dir: Path, project_root: Path, preflight, log):
        self.ssh = ssh_manager
        self._lab_dir = lab_dir
        self._project_root = project_root
        self._preflight = preflight
        self._log = log

    def run_from_yaml(self, yaml_path: str, out_dir: str, cancel_event=None):
        try:
            loader, Runner = _import_orchestrator()
        except Exception as e:
            raise RuntimeError(f"Orquestrador ausente: {e}")

        try:
            try:
                self._preflight.ensure(["attacker", "sensor", "victim"])
            except Exception as e:
                self._log(f"[Preflight] Aviso: {e}")

            exp = loader(str(yaml_path))
            runner = Runner(self.ssh, self._lab_dir)

            try:
                zip_path = runner.run(exp, out_dir=self._project_root / "data",
                                      run_pre_etl=True, cancel_event=cancel_event)
            except TypeError:
                zip_path = runner.run(exp, out_dir=self._project_root / "data",
                                      run_pre_etl=True)
            return str(zip_path)
        except Exception as e:
            raise

class MainWindow(QWidget):
    log_line = Signal(str)

    def __init__(self):
        super().__init__()
        self._guide_dialog = None
        self.setWindowTitle("VagrantLabUI — ML IDS Lab")
        self._ssh_tmux_sessions = {}
        self._workers = set()
        self._workers_lock = threading.RLock()
        self.warmup = WarmupCoordinator(warmup_window_s=30)
        self.log_line.connect(self._append_log_gui)

        try:
            screen = QApplication.screenAt(QCursor.pos())
            if screen:
                self.setGeometry(screen.geometry())
            self.showMaximized()
        except Exception as e:
            print(f"Falha ao maximizar janela: {e}")
            self.resize(600, 600)

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

        # --- Dataset Controller (toggle Gerar/Cancelar) ---
        try:
            from app.core.dataset_controller import DatasetController
        except Exception:
            raise ImportError("Módulos do DatasetController não encontrados em app/core/dataset_controller.py")
        self._ds_shim = UiRunnerShim(self.ssh, self.lab_dir, self.project_root, self.preflight, self._append_log)
        self._ds_controller = DatasetController(self._ds_shim)

        # UI feedback ao iniciar/terminar
        def _ds_started():
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

        def _ds_finished(status: str):
            try:
                self.global_progress.setVisible(False)
                if hasattr(self, "_ds_spinner") and self._ds_spinner:
                    self._ds_spinner.stop("Gerar Dataset (YAML)")
                self.btn_generate_dataset.setText("Gerar Dataset (YAML)")
                self._append_log(f"[Dataset] Finalizado com status: {status}")
            except Exception as e:
                self._append_log(f"[WARN] finish dataset ui: {e}")

        self._ds_controller.started.connect(_ds_started)
        self._ds_controller.finished.connect(_ds_finished)

        self.current_yaml_path = (self.project_root / "lab" / "experiments" / "exp_all.yaml")
        self._yaml_selected_by_user = False
        self._ensure_experiment_presets()

        self._build_ui()
        self._load_theme()

    def _ssh_paste(self, name: str, command: str):
        """
        Abre (se necessário) um terminal externo já anexado a um tmux persistente e envia `command`.
        Agora detecta janela fechada (sem clientes anexados) e reabre automaticamente.
        """
        try:
            host = (name or "attacker").strip().lower()
            if host == "vitima":
                host = "victim"

            st = self.vagrant.status_by_name(host)
            if st != "running":
                self._append_log(f"[WARN] {host} não está 'running' (rode: vagrant up {host}).")
                return

            try:
                self.vagrant.wait_ssh_ready(host, str(self.lab_dir), attempts=10, delay_s=3)
            except Exception as e:
                self._append_log(f"[WARN] SSH ainda não pronto em {host}: {e}")
                return

            session = f"guide_{host}"

            # 1) Garante tmux instalado e sessão criada (idempotente)
            try:
                self.ssh.run_command(host, "which tmux || sudo apt-get update && sudo apt-get install -y tmux",
                                     timeout=60)
            except Exception as e:
                self._append_log(f"[WARN] tmux não disponível em {host}: {e}")
            try:
                # cria se não existir; se existir, o stderr pode ter 'duplicate session' (ok)
                self.ssh.run_command(host, f"tmux new-session -d -s {session} || true", timeout=20)
            except Exception as e:
                self._append_log(f"[WARN] falha ao garantir sessão tmux em {host}: {e}")

            # 2) Descobre se há cliente anexado; se não houver, reabrimos o terminal
            attached = 0
            try:
                out = (self.ssh.run_command(
                    host,
                    f'tmux display-message -p -t {session} "#{ {"session_attached"} }"',
                    timeout=8
                ) or "").strip()
                # Fallback caso a forma acima não funcione
                if not out or not out.isdigit():
                    out = (self.ssh.run_command(
                        host, f"tmux list-clients -t {session} 2>/dev/null | wc -l", timeout=8
                    ) or "0").strip()
                attached = int(out or "0")
            except Exception as e:
                self._append_log(f"[WARN] não foi possível consultar clientes do tmux:{session} em {host}: {e}")
                attached = 0

            need_open = attached <= 0

            # 3) Decide abrir/reativar o terminal local
            proc = None
            try:
                rec = self._ssh_tmux_sessions.get(host) or {}
                old_proc = rec.get("proc")
                if old_proc is not None:
                    try:
                        # Se o processo do terminal morreu, consideramos como fechado
                        if hasattr(old_proc, "poll") and old_proc.poll() is not None:
                            need_open = True
                    except Exception:
                        need_open = True
            except Exception:
                pass

            if need_open:
                self._append_log(f"Abrindo terminal SSH (tmux:{session}) para {host}…")
                try:
                    # Agora open_external_terminal retorna o processo do terminal
                    proc = self.ssh.open_external_terminal(host, tmux_session=session)
                    # breve respiro para anexar
                    time.sleep(1.0)
                    # atualiza nosso registry local
                    self._ssh_tmux_sessions[host] = {"session": session, "opened": True, "proc": proc}
                except Exception as e:
                    self._append_log(f"[ERRO] Falha ao abrir SSH externo: {e}")
                    return
            else:
                self._append_log(f"[SSH] Terminal já anexado ao tmux:{session} em {host} (clientes={attached}).")

            # 4) Envia o comando para a sessão tmux (se houver payload)
            try:
                cmd = (command or "").strip().replace("\r\n", "\n")
                if cmd:
                    quoted = shlex.quote(cmd)
                    self.ssh.run_command(host, f"tmux send-keys -t {session} {quoted} C-m", timeout=20)
                    self._append_log(f"[SSH] Comando enviado ao tmux:{session} em {host}.")
                else:
                    self._append_log(f"[SSH] Sessão tmux:{session} pronta em {host} (sem comando para enviar).")
            except Exception as e:
                self._append_log(f"[WARN] Falha ao enviar comando ao tmux em {host}: {e}")

        except Exception as e:
            self._append_log(f"[ERRO] _ssh_paste falhou: {e}")

    def _with_ui_lock(self, fn, busy_msg: str):
        """
        Desabilita botões e mostra 'carregando' enquanto executa fn.
        """

        def wrapper():
            try:
                self._set_busy(True, busy_msg)
                fn()
            except Exception as e:
                self._append_log(f"[ERRO] {busy_msg}: {e}")
            finally:
                self._set_busy(False)

        return wrapper

    def on_click_status(self):
        """
        Handler do botão Status — AGORA com Preflight obrigatório.
        """
        @_self_contained
        def run():
            names = ["attacker", "sensor", "victim"]
            try:
                self.preflight.ensure(names)
                states = self.vagrant.status()
                self._apply_status_to_cards(states)
                running = [n for n in names if self.vagrant.status_by_name(n) == "running"]
                for i, n in enumerate(running):
                    delay_ms = 1200 * i
                    QTimer.singleShot(delay_ms, lambda name=n: self._spawn_info_update(name, "running"))
            except Exception as e:
                self._append_log(f"[ERRO] Status: {e}")

        self._with_ui_lock(run, "Atualizando status")()

    def _set_busy(self, busy: bool, msg: str = ""):
        try:
            self.btn_write.setEnabled(not busy)
            self.btn_up_all.setEnabled(not busy)
            self.btn_status.setEnabled(not busy)
            self.btn_halt_all.setEnabled(not busy)
            self.btn_destroy_all.setEnabled(not busy)
            self.btn_preflight.setEnabled(not busy)
            self.btn_yaml_designer.setEnabled(not busy)
            self.btn_pick_yaml.setEnabled(not busy)
            self.btn_generate_dataset.setEnabled(not busy)
            self.btn_open_data.setEnabled(not busy)
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

    def _wire_button_with_worker(self, btn: QPushButton, worker, active_label: str, idle_label: str):
        """
        Coloca spinner + desabilita o botão durante o worker e restaura ao final.
        """
        try:
            spinner = _SpinnerAnimator(btn, active_label)
            spinner.start()

            def _restore():
                try:
                    spinner.stop(idle_label)
                except Exception as e:
                    self._append_log(f"[WARN] restore botão: {e}")

            worker.done.connect(_restore)
            worker.error.connect(lambda msg: _restore())
        except Exception as e:
            self._append_log(f"[WARN] _wire_button_with_worker: {e}")

    def _build_ui(self):
        layout = QVBoxLayout(self)

        self.status_bar = QLabel("")
        self.status_bar.setObjectName("statusBar")
        self.status_bar.setStyleSheet("padding: 4px;")
        layout.addWidget(self.status_bar)

        self.global_progress = QProgressBar()
        self.global_progress.setRange(0, 0)
        self.global_progress.setVisible(False)
        layout.addWidget(self.global_progress)

        # Ações principais
        actions = QHBoxLayout()
        self.btn_write = QPushButton("Gerar Vagrantfile")
        self.btn_up_all = QPushButton("Subir todas")
        self.btn_status = QPushButton("Status")
        self.btn_halt_all = QPushButton("Halt todas")
        self.btn_destroy_all = QPushButton("Destroy todas")
        self.btn_preflight = QPushButton("Preflight")

        for b in [self.btn_write, self.btn_up_all, self.btn_status,
                  self.btn_halt_all, self.btn_destroy_all, self.btn_preflight]:
            actions.addWidget(b)
        layout.addLayout(actions)

        # Bloco de Dataset & Experimentos
        ds_actions = QHBoxLayout()
        self.btn_yaml_designer = QPushButton("Designer (YAML)")
        self.btn_pick_yaml = QPushButton("Escolher YAML")
        self.btn_generate_dataset = QPushButton("Gerar Dataset (YAML)")
        self.btn_open_guide = QPushButton("Guia do Experimento")
        self.btn_open_data = QPushButton("Abrir pasta data")
        for b in [self.btn_yaml_designer, self.btn_pick_yaml, self.btn_generate_dataset, self.btn_open_guide, self.btn_open_data]:
            ds_actions.addWidget(b)
        layout.addLayout(ds_actions)

        self.btn_open_guide.setObjectName("btnOpenGuide")

        # Tooltips
        self.btn_write.setToolTip("Gera/atualiza o Vagrantfile do laboratório")
        self.btn_up_all.setToolTip("Sobe attacker, sensor e victim (com aquecimento automático)")
        self.btn_status.setToolTip("Consulta o status das VMs")
        self.btn_halt_all.setToolTip("Desliga as VMs (halt)")
        self.btn_destroy_all.setToolTip("Destrói as VMs (irreversível)")
        self.btn_preflight.setToolTip("Roda validações essenciais do lab")

        self.btn_yaml_designer.setToolTip("Abrir designer visual de experimentos (YAML)")
        self.btn_pick_yaml.setToolTip("Escolher um arquivo YAML de experimento")
        self.btn_generate_dataset.setToolTip("Executa o experimento e empacota o dataset")
        self.btn_open_data.setToolTip("Abrir a pasta dos datasets gerados")
        self.btn_open_guide.setToolTip("Mostra um passo-a-passo baseado no YAML selecionado")

        # Cards por VM
        gb = QGroupBox("Máquinas do Lab")
        grid = QGridLayout()
        self.cards = {}
        for i, m in enumerate(self.cfg.machines):
            card = self._machine_card(m.name)
            grid.addWidget(card, i // 3, i % 3)
            self.cards[m.name] = card
        gb.setLayout(grid)
        layout.addWidget(gb)

        # Logs
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view, stretch=1)

        # Conexões
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

    def _machine_card(self, name: str) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        title = QLabel(f"{name}")
        title.setStyleSheet("font-size: 16pt; color:#00ffd1; font-weight:600;")
        v.addWidget(title)

        status = QLabel("●")
        status.setObjectName("statusDot")
        status.setProperty("status", "unknown")
        v.addWidget(status)

        pill_container = QWidget()
        pill_flow = FlowLayout(pill_container, hspacing=8, vspacing=8)
        pill_container.setLayout(pill_flow)

        pill_so = InfoPill("SO", "—", kind="so", parent=pill_container)
        pill_host = InfoPill("Host", "—", kind="host", parent=pill_container)
        pill_guest = InfoPill("Guest", "—", kind="guest", parent=pill_container)

        pill_flow.addWidget(pill_so)
        pill_flow.addWidget(pill_host)
        pill_flow.addWidget(pill_guest)

        v.addWidget(pill_container)

        row = QHBoxLayout()
        b_up = QPushButton("Up")
        b_status = QPushButton("Status")
        b_halt = QPushButton("Halt")
        b_restart = QPushButton("Restart")
        b_destroy = QPushButton("Destroy")
        b_ssh = QPushButton("SSH")

        row.addWidget(b_up)
        row.addWidget(b_status)
        row.addWidget(b_halt)
        row.addWidget(b_restart)
        row.addWidget(b_destroy)
        row.addWidget(b_ssh)
        v.addLayout(row)

        # Ligações
        b_up.clicked.connect(lambda: self._run_vagrant(self._on_up_vm, name, b_up, "Subindo…", "Up"))
        b_status.clicked.connect(lambda: self._run_status_by_name(name, b_status))
        b_halt.clicked.connect(lambda: self._run_vagrant(self.vagrant.halt, name, b_halt, "Halt…", "Halt"))
        b_restart.clicked.connect(lambda: self._on_restart_vm(name, b_restart))
        b_destroy.clicked.connect(
            lambda: self._run_vagrant(self.vagrant.destroy, name, b_destroy, "Destroy…", "Destroy"))
        b_ssh.clicked.connect(lambda: self._ssh(name, b_ssh))

        w.statusDot = status
        w.pills = {"so": pill_so, "host": pill_host, "guest": pill_guest}
        return w

    def _on_up_vm(self, name: str, btn: QPushButton = None):
        def gen():
            yield f"[Up] Subindo {name}..."
            for ln in self.vagrant.up(name):
                yield ln
            try:
                self.vagrant.wait_ssh_ready(name, str(self.lab_dir), attempts=12, delay_s=3)
                yield f"[Up] {name} está 'running' e SSH pronto."
                self.warmup.mark_boot(name)
                yield f"[Warmup] {name}: janela de aquecimento iniciada (30s)."
            except Exception as e:
                yield f"[Up] {name} 'running' porém SSH ainda não respondeu: {e}"

        worker = Worker(gen)
        worker.line.connect(self._append_log)

        def finalize():
            try:
                out = self.vagrant.status()
                self._apply_status_to_cards(out)
            except Exception as e:
                self._append_log(f"[Up] Falha ao atualizar status: {e}")

        worker.done.connect(finalize)
        worker.error.connect(lambda msg: self._append_log(f"[ERRO] Up {name}: {msg}"))

        if btn is not None:
            self._wire_button_with_worker(btn, worker, "Subindo…", "Up")

        self._keep_worker(worker, tag=f"up:{name}")
        worker.start()

    def _append_log(self, text: str):
        """
        Log seguro para threads:
        - Sempre escreve no logger (thread-safe).
        - Se for GUI thread, escreve na UI direto.
        - Se for worker thread, emite um sinal que a GUI thread consome.
        """
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
        """Executa exclusivamente na GUI thread."""
        try:
            self.log_view.appendPlainText(formatted)
        except Exception as e:
            import logging
            logging.error(f"Falha ao atualizar log_view: {e}")

    def _run_vagrant(self, fn, name=None, btn: QPushButton | None = None, active_label: str | None = None, idle_label: str | None = None):
        try:
            fn_name = getattr(fn, "__name__", "").lower()
            if fn_name in ("halt", "destroy"):
                self._append_log(f"[Thread] Parando threads antes de '{fn_name}'…")
                self._quiesce_background(reason=fn_name, timeout_s=6)
        except Exception as e:
            self._append_log(f"[WARN] _run_vagrant(quiesce): {e}")

        worker = Worker(fn, name) if name else Worker(fn)
        worker.line.connect(self._append_log)
        worker.error.connect(lambda msg: self._append_log(f"[ERRO] {msg}"))
        worker.done.connect(lambda: self._append_log("[OK] Finalizado."))
        if btn is not None:
            self._wire_button_with_worker(btn, worker, active_label or "Executando…", idle_label or btn.text())
        self._keep_worker(worker, tag=f"vagrant:{fn.__name__}")
        worker.start()

    def _ssh(self, name: str, btn: QPushButton | None = None):
        """
        Abre o SSH da VM em uma thread (Worker) para não travar a UI.
        Faz as validações mínimas (running + wait_ssh_ready) e chama o SSHManager.
        """
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
        """
        Reinicia a VM 'name' com segurança:
        - Tenta vagrant reload (se disponível no VagrantManager).
        - Fallback para halt + up quando reload não existir.
        - Aguarda SSH ficar pronto e marca janela de aquecimento.
        - Atualiza o status do card ao final.
        """

        def gen():
            try:
                self._append_log(f"[Restart] Reiniciando {name}…")

                # Quiesce threads ativas (como em halt/destroy)
                try:
                    self._append_log("[Thread] Parando threads antes de 'reload'…")
                    self._quiesce_background(reason="reload", timeout_s=6)
                except Exception as e:
                    self._append_log(f"[WARN] reload quiesce: {e}")

                # Se não está running, apenas "Up"
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
                    # Tenta reload nativo se existir
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

                # Aguarda SSH e marca janela de aquecimento
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

            # spinner no botão
            if btn is not None:
                self._wire_button_with_worker(btn, worker, "Restart…", "Restart")

            # ao terminar, atualiza status/infos do card
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
        """
        Agora funciona em modo toggle:
        - Se NÃO estiver rodando => inicia geração (usa DatasetController).
        - Se JÁ estiver rodando => solicita cancelamento imediato.
        """
        try:
            w = getattr(self._ds_controller, "_worker", None)
            if w and w.is_alive():
                self._append_log("[Dataset] Cancelamento solicitado pelo usuário.")
                try:
                    self._ds_controller.cancel()
                except Exception as e:
                    self._append_log(f"[ERRO] Cancel: {e}")
                return

            # iniciar
            yaml_path = str(self.current_yaml_path)
            out_dir = str(self.project_root / "data")
            self._ds_controller.start(yaml_path, out_dir)

        except Exception as e:
            self._append_log(f"[ERRO] Dataset: {e}")

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
        """
        Retorna (os_text, host_endpoint, guest_ip)
        - os_text: agora usa _query_os_friendly(name) quando a VM está 'running',
                   gerando algo como 'Ubuntu 16.04 LTS (x86_64, kernel 4.15.0)'.
        - host_endpoint: 'Host:Port' via ssh-config quando rodando; senão '—'.
        - guest_ip: calculado do config (ip_base + octeto).
        """
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
            if hasattr(card, "pills"):
                card.pills["so"].setValue(os_text)
                card.pills["host"].setValue(host_endpoint)
                card.pills["guest"].setValue(guest_ip)
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
        """
        Monta o contexto usado pelo Vagrantfile a partir do config principal,
        ajustando o IP da vítima conforme o YAML (se existir).
        - Base: self.cfg.to_template_ctx()
        - Se YAML tiver targets.victim_ip e corresponder ao ip_base do lab,
          atualiza o ip_last_octet da máquina 'victim' no ctx.
        """
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

        # Finaliza o ctx para o template
        ctx["machines"] = machines
        ctx["ip_base"] = getattr(self.cfg, "ip_base", ctx.get("ip_base"))
        return ctx

    def _query_os_friendly(self, name: str, timeout: int = 12) -> str:
        """
        Retorna um nome de SO amigável, ex.:
        - Ubuntu 16.04 LTS (x86_64, kernel 4.15.0)
        - Kali GNU/Linux Rolling (x86_64, kernel 6.10.9)
        - Windows Server 2019 10.0 (64-bit, build 17763)
        """
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

    def on_yaml_designer(self):  # << NOVO
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
        """
        Se a VM estiver 'running', registra o mark_boot(name) para o coordenador de warm-up.
        """
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
        """
        Sobe a VM 'name' e, ao detectar 'running', registra o warmup.mark_boot(name).
        Também dá um pequeno preflight para estabilizar o sshd.
        """
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
        """
        Pede para todas as threads pararem antes de operações destrutivas (halt/destroy).
        Espera até timeout e então força término se necessário.
        """
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
                        # QThread.wait recebe ms
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

def _self_contained(fn):
    """
    Decorator simples para logar início/fim de ações do UI thread.
    """
    def inner(*args, **kwargs):
        logger = setup_logger(LOG_DIR)
        logger.info(f"[UI] Iniciando {fn.__name__}...")
        try:
            return fn(*args, **kwargs)
        finally:
            logger.info(f"[UI] Finalizado {fn.__name__}.")
    return inner

def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
