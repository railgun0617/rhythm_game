from __future__ import annotations

import os
import sys
from pathlib import Path


def _detect_project_root() -> Path:
    """Return the folder containing assets/config when running from source or PyInstaller."""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled)
    override = os.environ.get("RHYTHM_MASTER_HOME")
    if override:
        try:
            return Path(override).expanduser().resolve()
        except OSError:
            pass
    return Path(__file__).resolve().parent


PROJECT_ROOT = _detect_project_root()
ASSETS_DIR = PROJECT_ROOT / "assets"
CHARTS_DIR = PROJECT_ROOT / "charts"
CONFIG_FILE = PROJECT_ROOT / "config.json"
SCORES_DIR = PROJECT_ROOT / "scores"
LOG_DIR = Path(os.environ.get("RHYTHM_MASTER_LOG_DIR", PROJECT_ROOT / "logs"))


def ensure_runtime_dirs() -> None:
    for folder in (CHARTS_DIR, SCORES_DIR, LOG_DIR):
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


ensure_runtime_dirs()
