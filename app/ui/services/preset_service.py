from __future__ import annotations
from typing import Callable
from pathlib import Path
import logging

class PresetBootstrapper:
    def __init__(self, *, project_root: Path, append_log: Callable[[str], None], logger: logging.Logger):
        self.project_root = Path(project_root)
        self.append_log = append_log
        self.logger = logger

    def ensure(self) -> None:
        try:
            exp_dir = self.project_root / "app" / "templates"
            exp_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.append_log(f"[WARN] Falha ao garantir presets: {e}")
