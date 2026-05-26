from __future__ import annotations

import sqlite3
from pathlib import Path
from config import settings

_DB_PATH: Path | None = None


def _resolve_path() -> Path:
    global _DB_PATH
    if _DB_PATH is None:
        raw = settings.get("database.path", "archivio.db")
        _DB_PATH = Path(raw)
    return _DB_PATH


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(_resolve_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema():
    schema = Path(__file__).parent / "schema.sql"
    conn = get_connection()
    with conn:
        conn.executescript(schema.read_text(encoding="utf-8"))
    conn.close()
