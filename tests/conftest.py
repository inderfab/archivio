import sqlite3
import pytest
from pathlib import Path
from db import connection


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(connection, "_DB_PATH", db_file)
    connection.init_schema()
    conn = connection.get_connection()
    yield conn
    conn.close()


@pytest.fixture
def sample_files(tmp_path) -> Path:
    (tmp_path / "plan.txt").write_text("Grundriss Erdgeschoss\nWohnfläche 120qm", encoding="utf-8")
    (tmp_path / "notes.md").write_text("# Besprechung\nProjekt läuft gut.", encoding="utf-8")
    (tmp_path / "binary.xyz").write_bytes(b"\x00\x01\x02")
    return tmp_path
