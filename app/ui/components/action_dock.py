# -*- coding: utf-8 -*-
"""
ActionDock (Matrix Edition) — desacoplado do main.

Mantém compatibilidade com main.py:
- Atributos: btn_write, btn_up_all, btn_status, btn_halt_all, btn_destroy_all, btn_preflight
             btn_yaml_designer, btn_pick_yaml, btn_generate_dataset, btn_open_guide, btn_open_data
             status_bar
- objectNames: "ActionDock", "dockTitle", "dockSubtitle", "statusBar"

Foco TCC: agrupar ações de Infra e Dataset/Experimentos.
"""
import logging
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QLabel, QGroupBox, QVBoxLayout as QV, QPushButton
)
from PySide6.QtCore import Qt

logger = logging.getLogger("[ActionDock]")
if not hasattr(logger, "warn"):
    logger.warn = logger.warning  # compat

class ActionDockWidgetExt(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        try:
            self.setObjectName("ActionDock")
            self.setFrameShape(QFrame.NoFrame)

            root = QVBoxLayout(self)
            root.setContentsMargins(12, 12, 12, 12)
            root.setSpacing(12)

            title = QLabel("VagrantLabUI")
            title.setObjectName("dockTitle")
            subtitle = QLabel("ML IDS Lab — Matrix Edition")
            subtitle.setObjectName("dockSubtitle")
            root.addWidget(title)
            root.addWidget(subtitle)
            root.addSpacing(6)

            infra = QGroupBox("Infra")
            iv = QV(infra)
            self.btn_write = QPushButton("Gerar Vagrantfile")
            self.btn_up_all = QPushButton("Subir todas")
            self.btn_status = QPushButton("Status")
            self.btn_halt_all = QPushButton("Halt todas")
            self.btn_destroy_all = QPushButton("Destroy todas")
            self.btn_preflight = QPushButton("Preflight")
            for b in (self.btn_write, self.btn_up_all, self.btn_status,
                      self.btn_halt_all, self.btn_destroy_all, self.btn_preflight):
                iv.addWidget(b)
            root.addWidget(infra)

            exp = QGroupBox("Dataset & Experimentos")
            ev = QV(exp)
            self.btn_yaml_designer = QPushButton("Designer (YAML)")
            self.btn_pick_yaml = QPushButton("Escolher YAML")
            self.btn_generate_dataset = QPushButton("Gerar Dataset (YAML)")
            self.btn_open_guide = QPushButton("Guia do Experimento")
            self.btn_open_data = QPushButton("Abrir pasta data")
            for b in (self.btn_yaml_designer, self.btn_pick_yaml, self.btn_generate_dataset,
                      self.btn_open_guide, self.btn_open_data):
                ev.addWidget(b)
            root.addWidget(exp)

            root.addStretch(1)
            self.status_bar = QLabel("")
            self.status_bar.setObjectName("statusBar")

            logger.info("[ActionDock] Construído com sucesso.")
        except Exception as e:
            logger.error(f"[ActionDock] Erro ao construir: {e}")
