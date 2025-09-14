import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QPlainTextEdit, QFileDialog, QMessageBox, QComboBox
)

try:
    import yaml
except Exception as e:
    yaml = None

logger = logging.getLogger("[YAMLDesigner]")

class YAMLDesignerDialog(QDialog):
    def __init__(self, parent=None, initial_path: Path | None = None, experiments_dir: Path | None = None):
        super().__init__(parent)
        self.setWindowTitle("Designer de Experimentos (YAML)")
        self.setMinimumSize(800, 600)
        self.current_path = Path(initial_path) if initial_path else None
        self.experiments_dir = Path(experiments_dir) if experiments_dir else Path.cwd() / "lab" / "experiments"
        self.experiments_dir.mkdir(parents=True, exist_ok=True)

        self._build_ui()
        if self.current_path and self.current_path.exists():
            self._load_file(self.current_path)

    def _build_ui(self):
        lay = QVBoxLayout(self)

        top = QHBoxLayout()
        self.lbl_path = QLabel(str(self.current_path) if self.current_path else "Sem arquivo selecionado")
        self.lbl_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        btn_open = QPushButton("Abrir…")
        btn_save = QPushButton("Salvar")
        btn_save_as = QPushButton("Salvar como…")
        btn_validate = QPushButton("Validar YAML")
        top.addWidget(self.lbl_path, stretch=1)
        top.addWidget(btn_open)
        top.addWidget(btn_save)
        top.addWidget(btn_save_as)
        top.addWidget(btn_validate)
        lay.addLayout(top)

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("# Edite seu experimento aqui…")
        font = self.editor.font()
        font.setFamily("Consolas")
        font.setPointSize(11)
        self.editor.setFont(font)
        lay.addWidget(self.editor, stretch=1)

        presets = QHBoxLayout()
        presets.addWidget(QLabel("Preset:"))
        self.combo_presets = QComboBox()
        self.combo_presets.addItems([
            "exp_all.yaml",
            "exp_scan_brute.yaml",
            "exp_dos.yaml",
            "exp_heavy_syn.yaml",     # NOVO
            "exp_brute_http.yaml"     # NOVO
        ])
        btn_use_preset = QPushButton("Carregar no editor")
        btn_create_files = QPushButton("Criar arquivos de preset (se faltarem)")
        presets.addWidget(self.combo_presets)
        presets.addWidget(btn_use_preset)
        presets.addWidget(btn_create_files)
        lay.addLayout(presets)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        btn_close = QPushButton("Fechar")
        bottom.addWidget(btn_close)
        lay.addLayout(bottom)

        btn_open.clicked.connect(self._on_open)
        btn_save.clicked.connect(self._on_save)
        btn_save_as.clicked.connect(self._on_save_as)
        btn_validate.clicked.connect(self._on_validate)
        btn_use_preset.clicked.connect(self._on_use_preset)
        btn_create_files.clicked.connect(self._on_create_presets)
        btn_close.clicked.connect(self.accept)

    def _on_open(self):
        try:
            path, _ = QFileDialog.getOpenFileName(self, "Abrir experimento YAML",
                                                  str(self.experiments_dir), "YAML (*.yaml *.yml)")
            if not path:
                return
            p = Path(path)
            self._load_file(p)
            self.current_path = p
            self.lbl_path.setText(str(p))
            logger.info(f"[YAMLDesigner] Aberto: {p}")
        except Exception as e:
            logger.error(f"[YAMLDesigner] Falha ao abrir: {e}")
            QMessageBox.critical(self, "Erro", f"Falha ao abrir: {e}")

    def _on_save(self):
        try:
            if not self.current_path:
                return self._on_save_as()
            self.current_path.write_text(self.editor.toPlainText(), encoding="utf-8")
            logger.info(f"[YAMLDesigner] Salvo: {self.current_path}")
            QMessageBox.information(self, "OK", f"Arquivo salvo:\n{self.current_path}")
        except Exception as e:
            logger.error(f"[YAMLDesigner] Falha ao salvar: {e}")
            QMessageBox.critical(self, "Erro", f"Falha ao salvar: {e}")

    def _on_save_as(self):
        try:
            path, _ = QFileDialog.getSaveFileName(self, "Salvar como",
                                                  str(self.experiments_dir / "novo_experimento.yaml"),
                                                  "YAML (*.yaml *.yml)")
            if not path:
                return
            p = Path(path)
            p.write_text(self.editor.toPlainText(), encoding="utf-8")
            self.current_path = p
            self.lbl_path.setText(str(p))
            logger.info(f"[YAMLDesigner] Salvo como: {p}")
            QMessageBox.information(self, "OK", f"Arquivo salvo:\n{p}")
        except Exception as e:
            logger.error(f"[YAMLDesigner] Falha ao salvar como: {e}")
            QMessageBox.critical(self, "Erro", f"Falha ao salvar como: {e}")

    def _on_validate(self):
        try:
            if yaml is None:
                raise RuntimeError("PyYAML não está instalado.")
            data = yaml.safe_load(self.editor.toPlainText())
            if not isinstance(data, dict):
                raise ValueError("Estrutura YAML inválida (esperado mapeamento).")
            missing = []
            if "exp_id" not in data:
                missing.append("exp_id")
            if "targets" not in data or "victim_ip" not in data["targets"]:
                missing.append("targets.victim_ip")
            if "actions" not in data or not isinstance(data["actions"], list) or not data["actions"]:
                missing.append("actions[]")
            if missing:
                raise ValueError(f"Campos faltando: {', '.join(missing)}")
            QMessageBox.information(self, "Válido", "YAML válido para execução.")
            logger.info("[YAMLDesigner] Validação OK.")
        except Exception as e:
            logger.error(f"[YAMLDesigner] Validação falhou: {e}")
            QMessageBox.warning(self, "Inválido", f"Problema no YAML:\n{e}")

    def _on_use_preset(self):
        try:
            name = self.combo_presets.currentText()
            p = self.experiments_dir / name
            if not p.exists():
                raise FileNotFoundError(f"Preset não encontrado: {p}")
            self._load_file(p)
            self.current_path = p
            self.lbl_path.setText(str(p))
            logger.info(f"[YAMLDesigner] Carregado preset: {p}")
        except Exception as e:
            logger.error(f"[YAMLDesigner] Falha ao carregar preset: {e}")
            QMessageBox.critical(self, "Erro", f"Falha ao carregar preset:\n{e}")

    def _on_create_presets(self):
        try:
            created = []
            mapping = {
                "exp_all.yaml": self._default_exp_all(),
                "exp_scan_brute.yaml": self._default_exp_scan_brute(),
                "exp_dos.yaml": self._default_exp_dos(),
                "exp_heavy_syn.yaml": self._default_exp_heavy_syn(),       # NOVO
                "exp_brute_http.yaml": self._default_exp_brute_http(),     # NOVO
            }
            for name, content in mapping.items():
                p = self.experiments_dir / name
                if not p.exists():
                    p.write_text(content, encoding="utf-8")
                    created.append(name)
            if created:
                QMessageBox.information(self, "OK", "Criados: " + ", ".join(created))
                logger.info(f"[YAMLDesigner] Presets criados: {created}")
            else:
                QMessageBox.information(self, "OK", "Todos os presets já existem.")
        except Exception as e:
            logger.error(f"[YAMLDesigner] Falha ao criar presets: {e}")
            QMessageBox.critical(self, "Erro", f"Falha ao criar presets:\n{e}")

    def _load_file(self, path: Path):
        try:
            text = path.read_text(encoding="utf-8")
            self.editor.setPlainText(text)
        except Exception as e:
            logger.error(f"[YAMLDesigner] Erro ao ler arquivo: {e}")
            raise

    # Presets (iguais aos do orquestrador)
    def _default_exp_all(self) -> str:
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

    def _default_exp_scan_brute(self) -> str:
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

    def _default_exp_dos(self) -> str:
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

    def _default_exp_heavy_syn(self) -> str:
        return (
            "exp_id: \"EXP_HEAVY_SYN\"\n"
            "targets:\n"
            "  victim_ip: \"192.168.56.20\"\n"
            "capture:\n"
            "  rotate_seconds: 120\n"
            "  rotate_size_mb: 200\n"
            "  zeek_rotate_seconds: 1200\n"
            "actions:\n"
            "  - name: \"hping3_syn\"\n"
            "    params:\n"
            "      dst_port: 8080\n"
            "      rate_pps: 800\n"
            "      duration_s: 120\n"
        )

    def _default_exp_brute_http(self) -> str:
        return (
            "exp_id: \"EXP_BRUTE_HTTP\"\n"
            "targets:\n"
            "  victim_ip: \"192.168.56.20\"\n"
            "capture:\n"
            "  rotate_seconds: 300\n"
            "  rotate_size_mb: 100\n"
            "  zeek_rotate_seconds: 3600\n"
            "actions:\n"
            "  - name: \"hydra_http_post\"\n"
            "    params:\n"
            "      path: \"/login\"\n"
            "      user: \"webuser\"\n"
            "      pass_list: [\"secret\", \"123456\", \"admin\", \"password\"]\n"
            "      fail_regex: \"invalid\"\n"
            "      port: 8081\n"
            "      output: \"exp_brute_http.hydra\"\n"
            "      threads: 4\n"
        )
