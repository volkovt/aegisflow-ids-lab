# app/core/pathing.py
import os
import logging
from pathlib import Path

logger = logging.getLogger("[Pathing]")

def get_project_root(start: Path | None = None) -> Path:
    p = (start or Path(__file__)).resolve()
    for cand in [p] + list(p.parents):
        # Heurísticas simples para detectar a raiz do projeto
        if (cand / "manage_lab.py").exists() and (cand / "app").exists():
            return cand
    return Path.cwd()

def find_config(explicit: Path | None = None) -> Path:
    # 1) variável de ambiente opcional
    env = os.getenv("VAGRANTLAB_CONFIG")
    if env:
        path = Path(env).expanduser().resolve()
        if path.exists():
            logger.info(f"[Pathing] Usando config via VAGRANTLAB_CONFIG: {path}")
            return path

    # 2) candidato explícito (quando informado)
    if explicit:
        explicit = explicit.expanduser().resolve()
        if explicit.exists():
            logger.info(f"[Pathing] Usando config explícito: {explicit}")
            return explicit

    # 3) locais comuns: CWD e raiz do projeto
    candidates = [
        Path.cwd() / "config.yaml",
        get_project_root() / "config.yaml",
    ]
    for c in candidates:
        if c.exists():
            logger.info(f"[Pathing] Usando config encontrado: {c.resolve()}")
            return c.resolve()

    # 4) falha — mensagem clara
    places = " | ".join(str(c) for c in candidates)
    raise FileNotFoundError(
        f"config.yaml não encontrado. Procurei em: {places} "
        f"(ou defina VAGRANTLAB_CONFIG com o caminho do arquivo)."
    )
