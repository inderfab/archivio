from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from config import settings

_DB_PATH: Path | None = None


def _resolve_path() -> Path:
    global _DB_PATH
    if _DB_PATH is None:
        raw = settings.get("database.path", "archivio.db")
        path = Path(raw)
        if not path.is_absolute():
            data_dir = os.environ.get("ARCHIVIO_DATA_DIR")
            if data_dir:
                path = Path(data_dir) / path
        _DB_PATH = path
    return _DB_PATH


def get_connection() -> sqlite3.Connection:
    # timeout=30: bei parallelen Schreibzugriffen bis 30s auf Lock warten
    conn = sqlite3.connect(_resolve_path(), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema():
    from db import migrations
    schema = Path(__file__).parent / "schema.sql"
    conn = get_connection()
    with conn:
        conn.executescript(schema.read_text(encoding="utf-8"))
    migrations.run(conn)
    conn.close()
