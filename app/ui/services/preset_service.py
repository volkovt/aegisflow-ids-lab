from __future__ import annotations
from typing import Callable
from pathlib import Path
import logging

from app.core.default_presets import (
    preset_all,
    preset_scan_brute,
    preset_dos,
    preset_brute_http,
    preset_heavy_syn,
)


class PresetBootstrapper:
    def __init__(self, *, project_root: Path, append_log: Callable[[str], None], logger: logging.Logger):
        self.project_root = Path(project_root)
        self.append_log = append_log
        self.logger = logger

    def ensure(self) -> None:
        try:
            exp_dir = self.project_root / "lab" / "experiments"
            exp_dir.mkdir(parents=True, exist_ok=True)

            def write_if_missing(path: Path, content: str):
                if not path.exists():
                    path.write_text(content, encoding="utf-8")
                    self.append_log(f"[Dataset] Preset criado: {path}")

            write_if_missing(exp_dir / "exp_all.yaml", preset_all())
            write_if_missing(exp_dir / "exp_scan_brute.yaml", preset_scan_brute())
            write_if_missing(exp_dir / "exp_dos.yaml", preset_dos())
            write_if_missing(exp_dir / "exp_brute_http.yaml", preset_brute_http())
            write_if_missing(exp_dir / "exp_heavy_syn.yaml", preset_heavy_syn())
        except Exception as e:
            self.append_log(f"[WARN] Falha ao garantir presets: {e}")
