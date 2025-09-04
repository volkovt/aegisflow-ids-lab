import logging
from logging.handlers import RotatingFileHandler
from colorama import Fore, Style, init as colorama_init
from pathlib import Path

colorama_init()

def setup_logger(log_dir: Path, name: str = "VagrantLabUI") -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    # Console (neon)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)

    class NeonFormatter(logging.Formatter):
        def format(self, record):
            level = record.levelname
            color = {
                "INFO": Fore.CYAN,
                "WARNING": Fore.YELLOW,
                "ERROR": Fore.RED,
            }.get(level, Fore.MAGENTA)
            prefix = f"{color}[{level}] {Style.RESET_ALL}"
            return f"{prefix}{record.name}: {self.formatTime(record)} | linha {record.lineno} | {record.getMessage()}"

    ch.setFormatter(NeonFormatter())

    # Arquivo c/ rotação
    fh = RotatingFileHandler(log_dir / "lab.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s | %(name)s | %(levelname)s | %(message)s"))

    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger