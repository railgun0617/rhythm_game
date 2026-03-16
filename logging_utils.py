from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List

from runtime_paths import LOG_DIR

_CONFIGURED = False


def setup_logging(*, level: int = logging.INFO, enable_console: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    handlers: List[logging.Handler] = []
    log_dir = LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "rhythm_master.log"

    try:
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=2_000_000,
            backupCount=2,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(name)s: %(message)s")
        )
        handlers.append(file_handler)
    except OSError:
        enable_console = True

    if enable_console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
        handlers.append(console_handler)

    logging.basicConfig(level=level, handlers=handlers, force=True)
    _CONFIGURED = True


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(name)
