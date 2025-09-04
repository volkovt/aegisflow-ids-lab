import inspect
import os
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QPlainTextEdit, QGroupBox, QGridLayout, QMessageBox, QFileDialog
)
from PySide6.QtCore import QTimer

from app.core.logger_setup import setup_logger
from app.core.config_loader import load_config
from app.core.pathing import get_project_root, find_config
from app.core.preflight import run_preflight
from app.core.vagrant_manager import VagrantManager
from app.core.ssh_manager import SSHManager

import warnings

from app.core.workers.result_worker import ResultWorker
from app.core.workers.worker import Worker
from app.ui.flow_layout import FlowLayout
from app.ui.info_pills import InfoPill
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


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VagrantLabUI — ML IDS Lab")
        self._workers = set()
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

        # Caminho padrão do YAML de experimento
        self.current_yaml_path = (self.project_root / "experiments" / "exp_all.yaml")
        self._ensure_experiment_presets()

        self._build_ui()
        self._load_theme()

    def _keep_worker(self, w, tag=""):
        try:
            self._workers.add(w)
            try:
                w.done.connect(lambda: self._workers.discard(w))
            except Exception:
                pass
            self._append_log(f"[Thread] iniciado {tag or w}")
        except Exception as e:
            self._append_log(f"[WARN] _keep_worker: {e}")

    def _build_ui(self):
        layout = QVBoxLayout(self)

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
        self.btn_open_data = QPushButton("Abrir pasta data")
        for b in [self.btn_yaml_designer, self.btn_pick_yaml, self.btn_generate_dataset, self.btn_open_data]:
            ds_actions.addWidget(b)
        layout.addLayout(ds_actions)

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
        self.btn_up_all.clicked.connect(self.on_up_all)
        self.btn_status.clicked.connect(self.on_status)
        self.btn_halt_all.clicked.connect(self.on_halt_all)
        self.btn_destroy_all.clicked.connect(self.on_destroy_all)
        self.btn_preflight.clicked.connect(self.on_preflight)

        self.btn_yaml_designer.clicked.connect(self.on_yaml_designer)
        self.btn_pick_yaml.clicked.connect(self.on_pick_yaml)
        self.btn_generate_dataset.clicked.connect(self.on_generate_dataset)
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
        b_destroy = QPushButton("Destroy")
        b_ssh = QPushButton("SSH")

        row.addWidget(b_up)
        row.addWidget(b_status)
        row.addWidget(b_halt)
        row.addWidget(b_destroy)
        row.addWidget(b_ssh)
        v.addLayout(row)

        # Ligações
        b_up.clicked.connect(lambda: self._run_vagrant(self.vagrant.up, name))
        b_status.clicked.connect(lambda: self.status_by_name(name))
        b_halt.clicked.connect(lambda: self._run_vagrant(self.vagrant.halt, name))
        b_destroy.clicked.connect(lambda: self._run_vagrant(self.vagrant.destroy, name))
        b_ssh.clicked.connect(lambda: self._ssh(name))

        w.statusDot = status
        w.pills = {"so": pill_so, "host": pill_host, "guest": pill_guest}
        return w

    def _append_log(self, text: str):
        frame = inspect.currentframe().f_back
        lineno = frame.f_lineno
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted = f"{timestamp} | linha {lineno} | {text}"
        try:
            self.logger.info(text)
            self.log_view.appendPlainText(formatted)
        except Exception as e:
            import logging
            logging.error(f"Falha ao logar: {e}")

    def _run_vagrant(self, fn, name=None):
        worker = Worker(fn, name) if name else Worker(fn)
        worker.line.connect(self._append_log)
        worker.error.connect(lambda msg: self._append_log(f"[ERRO] {msg}"))
        worker.done.connect(lambda: self._append_log("[OK] Finalizado."))
        self._keep_worker(worker, tag=f"vagrant:{fn.__name__}")
        worker.start()

    def _ssh(self, name: str):
        try:
            status_out = self.vagrant.status()
            if (f"{name}" in status_out) and ("running" not in status_out.splitlines()[-1].lower()) and (
                    "running" not in status_out.lower()):
                self._append_log(f"[WARN] {name} não está 'running' (rode: vagrant up {name}).")
                return
            self._append_log(f"Abrindo terminal SSH externo para {name}...")
            self.ssh.open_external_terminal(name)
        except Exception as e:
            self._append_log(f"Falha ao abrir SSH externo: {e}")

    def on_write(self):
        try:
            from jinja2 import Environment, FileSystemLoader
            env = Environment(loader=FileSystemLoader(str(self.project_root / "app" / "templates")))
            vf = self.vagrant.write_vagrantfile(self.project_root / "app" / "templates", self.cfg.to_template_ctx())
            self._append_log(f"Vagrantfile gerado em: {vf}")
        except Exception as e:
            self._append_log(f"Erro ao gerar Vagrantfile: {e}")

    def on_up_all(self):
        self._run_vagrant(self.vagrant.up)

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

    def on_halt_all(self):
        self._run_vagrant(self.vagrant.halt)

    def on_destroy_all(self):
        confirm = QMessageBox.question(self, "Confirmar", "Destruir TODAS as VMs? Esta ação é irreversível.")
        if confirm == QMessageBox.Yes:
            self._run_vagrant(self.vagrant.destroy)

    def on_preflight(self):  # NOVO
        try:
            worker = Worker(run_preflight, self.project_root, self.lab_dir, self.cfg, self.vagrant, self.ssh)
            worker.line.connect(self._append_log)
            worker.error.connect(lambda msg: self._append_log(f"[ERRO] {msg}"))
            worker.done.connect(
                lambda: self._append_log("[OK] Preflight finalizado. Relatório em .logs/lab_preflight.txt"))
            worker.start()
        except Exception as e:
            self._append_log(f"Erro ao iniciar preflight: {e}")

    def on_pick_yaml(self):
        try:
            path, _ = QFileDialog.getOpenFileName(
                self, "Escolher YAML de experimento",
                str(self.current_yaml_path if self.current_yaml_path.exists() else (self.project_root / "experiments")),
                "YAML (*.yaml *.yml)"
            )
            if path:
                self.current_yaml_path = Path(path)
                self._append_log(f"[Dataset] YAML selecionado: {self.current_yaml_path}")
            else:
                self._append_log("[Dataset] YAML não alterado.")
        except Exception as e:
            self._append_log(f"[Dataset] Falha ao escolher YAML: {e}")

    def on_generate_dataset(self):
        try:
            loader, Runner = _import_orchestrator()
        except Exception as e:
            self._append_log(f"[ERRO] {e}")
            QMessageBox.critical(self, "Orquestrador ausente",
                                 "Os módulos do orquestrador não foram encontrados.\n"
                                 "Crie as pastas 'orchestrator/*' sugeridas anteriormente.")
            return

        def job():
            try:
                exp = loader(str(self.current_yaml_path))
                runner = Runner(self.ssh, self.lab_dir)
                zip_path = runner.run(exp, out_dir=self.project_root / "data", run_pre_etl=True)
                return str(zip_path)
            except Exception as e:
                raise RuntimeError(f"Geração de dataset falhou: {e}")

        w = ResultWorker(job)
        w.result.connect(lambda p: self._append_log(f"[Dataset] OK: {p}"))
        w.error.connect(lambda msg: self._append_log(f"[ERRO] Dataset: {msg}"))
        w.start()

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
                delay_ms = 300 * i
                QTimer.singleShot(delay_ms, lambda name=n, st="running": self._spawn_info_update(name, st))

        except Exception as e:
            self._append_log(f"[WARN] _apply_status_to_cards: {e}")

    def _spawn_info_update(self, name: str, state: str):
        def job():
            return (name, *self._collect_machine_details(name, state_hint=state))

        w = ResultWorker(job)
        w.result.connect(lambda res: self._set_card_info(*res))
        w.error.connect(lambda msg: self._append_log(f"[WARN] Info {name} falhou: {msg}"))
        self._keep_worker(w, tag=f"info:{name}")
        w.start()

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
            exp_dir = self.project_root / "experiments"
            exp_dir.mkdir(parents=True, exist_ok=True)

            def write_if_missing(path: Path, content: str):
                if not path.exists():
                    path.write_text(content, encoding="utf-8")
                    self._append_log(f"[Dataset] Preset criado: {path}")

            exp_all = exp_dir / "exp_all.yaml"
            write_if_missing(exp_all, self._preset_all())

            exp_scan_brute = exp_dir / "exp_scan_brute.yaml"
            write_if_missing(exp_scan_brute, self._preset_scan_brute())

            exp_dos = exp_dir / "exp_dos.yaml"
            write_if_missing(exp_dos, self._preset_dos())
        except Exception as e:
            self._append_log(f"[WARN] Falha ao garantir presets: {e}")

    def _preset_all(self) -> str:
        return (
            "exp_id: \"EXP_ALL\"\n"
            "targets:\n"
            "  victim_ip: \"192.168.56.20\"\n"
            "capture:\n"
            "  rotate_seconds: 300\n"
            "  rotate_size_mb: 100\n"
            "  zeek_rotate_seconds: 3600\n"
            "actions:\n"
            "  - name: \"nmap_scan\"\n"
            "    params:\n"
            "      output: \"exp_scan.nmap\"\n"
            "  - name: \"hydra_brute\"\n"
            "    params:\n"
            "      user: \"tcc\"\n"
            "      pass_list: [\"123456\", \"wrongpass\"]\n"
            "      output: \"exp_brute.hydra\"\n"
            "  - name: \"slowhttp_dos\"\n"
            "    params:\n"
            "      port: 8080\n"
            "      duration_s: 120\n"
            "      concurrency: 400\n"
            "      rate: 150\n"
            "      output_prefix: \"exp_dos\"\n"
        )

    def _preset_scan_brute(self) -> str:
        return (
            "exp_id: \"EXP_SCAN_BRUTE\"\n"
            "targets:\n"
            "  victim_ip: \"192.168.56.20\"\n"
            "capture:\n"
            "  rotate_seconds: 300\n"
            "  rotate_size_mb: 100\n"
            "  zeek_rotate_seconds: 3600\n"
            "actions:\n"
            "  - name: \"nmap_scan\"\n"
            "    params:\n"
            "      output: \"exp_scan.nmap\"\n"
            "  - name: \"hydra_brute\"\n"
            "    params:\n"
            "      user: \"tcc\"\n"
            "      pass_list: [\"123456\", \"wrongpass\"]\n"
            "      output: \"exp_brute.hydra\"\n"
        )

    def _preset_dos(self) -> str:
        return (
            "exp_id: \"EXP_DOS\"\n"
            "targets:\n"
            "  victim_ip: \"192.168.56.20\"\n"
            "capture:\n"
            "  rotate_seconds: 300\n"
            "  rotate_size_mb: 100\n"
            "  zeek_rotate_seconds: 3600\n"
            "actions:\n"
            "  - name: \"slowhttp_dos\"\n"
            "    params:\n"
            "      port: 8080\n"
            "      duration_s: 180\n"
            "      concurrency: 600\n"
            "      rate: 200\n"
            "      output_prefix: \"exp_dos\"\n"
        )

    def on_yaml_designer(self):  # << NOVO
        try:
            dlg = YAMLDesignerDialog(
                parent=self,
                initial_path=self.current_yaml_path if self.current_yaml_path.exists() else None,
                experiments_dir=self.project_root / "experiments"
            )
            dlg.exec()
            if dlg.current_path:
                self.current_yaml_path = dlg.current_path
                self._append_log(f"[Dataset] YAML atual: {self.current_yaml_path}")
        except Exception as e:
            self._append_log(f"[UI] Erro ao abrir Designer (YAML): {e}")


    def _load_theme(self):
        try:
            qss = (Path(__file__).parent / "futuristic.qss").read_text(encoding="utf-8")
            self.setStyleSheet(qss)
        except Exception as e:
            self.logger.warning(f"[UI] Falha ao carregar tema: {e}")


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
