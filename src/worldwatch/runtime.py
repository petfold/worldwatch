"""Runtime configuration: resolve paths and load the source set.

Environment (all optional, sensible defaults):
  WW_DB_PATH             SQLite database path
  WW_CONFIG_DIR          directory of per-source TOML stanzas
  WW_FINE_WINDOW_SECONDS raw_ring retention before consolidation (default 900);
                         smaller = surprise scored sooner, larger = more raw
                         resolution kept. See the geometric-cascade note.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from worldwatch import config as _config_pkg
from worldwatch.config.loader import SourceConfig, load_sources
from worldwatch.db import open_db

DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "worldwatch" / "worldwatch.db"
DEFAULT_FINE_WINDOW_SECONDS = 900
_PACKAGED_CONFIG_DIR = Path(_config_pkg.__file__).parent / "sources"


def db_path() -> Path:
    return Path(os.environ.get("WW_DB_PATH", str(DEFAULT_DB_PATH)))


def config_dir() -> Path:
    return Path(os.environ.get("WW_CONFIG_DIR", str(_PACKAGED_CONFIG_DIR)))


def fine_window_seconds() -> int:
    return int(os.environ.get("WW_FINE_WINDOW_SECONDS", DEFAULT_FINE_WINDOW_SECONDS))


def load() -> tuple[sqlite3.Connection, dict[str, SourceConfig]]:
    """Open (and migrate) the DB and load the configured sources."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(path)
    sources = load_sources(config_dir())
    return conn, sources
