"""Tests für db/backup.py::create_backup -- konsistente Kopie der Datenbank samt
Einstellungen auf einen frei wählbaren Zielordner (Datei-Server, externe Platte).

Der wichtigste Punkt hier ist nicht der Erfolgsfall, sondern dass ein Fehlschlag die
bisherige Sicherung NIE beschädigt: es gibt bewusst nur eine Kopie, und die muss auch
dann noch gültig sein, wenn der Lauf mittendrin scheitert."""
import json
import sqlite3

import pytest
import yaml

from config import settings
from db import backup, queries


def _fill(conn, projects=1, docs=2):
    for p in range(projects):
        pid = queries.insert_project(conn, f"P{p}", f"/scan/P{p}")
        for d in range(docs):
            queries.upsert_document(conn, {
                "project_id": pid, "hash": f"h{p}_{d}", "filename": f"f{d}.pdf",
                "extension": ".pdf", "filesize": 10, "modified_at": None,
                "source_type": "filesystem",
            })
    conn.commit()


def test_backup_creates_db_config_and_manifest(tmp_db, tmp_path):
    _fill(tmp_db)
    target = tmp_path / "server"
    target.mkdir()

    res = backup.create_backup(target)
    assert res["ok"] is True, res.get("error")

    d = target / backup.BACKUP_DIR_NAME
    assert (d / backup.DB_FILE).exists()
    assert (d / backup.CONFIG_FILE).exists()

    manifest = json.loads((d / backup.MANIFEST_FILE).read_text())
    assert manifest["stats"]["projects"] == 1
    assert manifest["stats"]["documents"] == 2
    assert manifest["server_version"] == backup.server_version()

    # Die Kopie muss eigenständig lesbar sein, nicht nur vorhanden
    conn = sqlite3.connect(d / backup.DB_FILE)
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
    conn.close()


def test_backup_strips_mail_passwords(tmp_db, tmp_path):
    """Die Sicherung landet auf einem Datei-Server, den im Büro viele lesen können."""
    settings.save({"mail_accounts": [
        {"label": "Büro", "host": "imap.example.com", "username": "u", "password": "geheim"},
    ]})
    target = tmp_path / "server"
    target.mkdir()

    assert backup.create_backup(target)["ok"] is True

    saved = yaml.safe_load(
        (target / backup.BACKUP_DIR_NAME / backup.CONFIG_FILE).read_text())
    assert saved["mail_accounts"][0]["password"] == ""
    assert saved["mail_accounts"][0]["username"] == "u"   # Rest bleibt erhalten
    # Die laufende Installation behält ihr Passwort
    assert settings.load_all()["mail_accounts"][0]["password"] == "geheim"


def test_backup_replaces_previous_one(tmp_db, tmp_path):
    _fill(tmp_db, docs=1)
    target = tmp_path / "server"
    target.mkdir()
    assert backup.create_backup(target)["ok"] is True

    queries.upsert_document(tmp_db, {
        "project_id": 1, "hash": "neu", "filename": "neu.pdf", "extension": ".pdf",
        "filesize": 1, "modified_at": None, "source_type": "filesystem"})
    tmp_db.commit()
    assert backup.create_backup(target)["ok"] is True

    conn = sqlite3.connect(target / backup.BACKUP_DIR_NAME / backup.DB_FILE)
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
    conn.close()
    # Keine Rückstände vom Einwechseln
    assert not (target / (backup.BACKUP_DIR_NAME + ".neu")).exists()
    assert not (target / (backup.BACKUP_DIR_NAME + ".alt")).exists()


def test_corrupt_copy_leaves_previous_backup_intact(tmp_db, tmp_path, monkeypatch):
    """Ohne quick_check würde eine schleichend defekte DB die letzte gute Kopie
    überschreiben -- genau das darf bei nur einer Sicherung nicht passieren."""
    _fill(tmp_db, docs=1)
    target = tmp_path / "server"
    target.mkdir()
    assert backup.create_backup(target)["ok"] is True
    vorher = (target / backup.BACKUP_DIR_NAME / backup.DB_FILE).read_bytes()

    monkeypatch.setattr(backup, "_quick_check", lambda p: "database disk image is malformed")
    res = backup.create_backup(target)

    assert res["ok"] is False
    assert "fehlerhaft" in res["error"]
    assert (target / backup.BACKUP_DIR_NAME / backup.DB_FILE).read_bytes() == vorher


def test_backup_fails_cleanly_on_missing_target(tmp_db, tmp_path):
    res = backup.create_backup(tmp_path / "gibt-es-nicht")
    assert res["ok"] is False
    assert "nicht erreichbar" in res["error"]


def test_backup_refuses_when_target_too_small(tmp_db, tmp_path, monkeypatch):
    import shutil as _shutil
    target = tmp_path / "server"
    target.mkdir()
    real = _shutil.disk_usage

    def fake(path):
        u = real(path)
        return type(u)(u.total, u.used, 1024) if str(path).startswith(str(target)) else u

    monkeypatch.setattr(backup.shutil, "disk_usage", fake)
    res = backup.create_backup(target)
    assert res["ok"] is False
    assert "Platz" in res["error"]


def test_state_file_records_success_and_resets_failures(tmp_db, tmp_path):
    target = tmp_path / "server"
    target.mkdir()
    backup.create_backup(tmp_path / "weg")           # scheitert
    assert backup.load_state()["consecutive_failures"] == 1
    backup.create_backup(target)                     # gelingt
    state = backup.load_state()
    assert state["last_ok"] is True
    assert state["consecutive_failures"] == 0
    assert state["target"] == str(target)


def test_interrupted_swap_is_recovered(tmp_db, tmp_path):
    """Bricht der Lauf zwischen den beiden Umbenennungen ab, liegt nur noch `.alt`
    da -- das ist dann der letzte gültige Stand und muss zurückkommen."""
    target = tmp_path / "server"
    target.mkdir()
    assert backup.create_backup(target)["ok"] is True
    (target / backup.BACKUP_DIR_NAME).rename(target / (backup.BACKUP_DIR_NAME + ".alt"))

    backup._recover_interrupted(target)

    assert (target / backup.BACKUP_DIR_NAME / backup.DB_FILE).exists()
    assert not (target / (backup.BACKUP_DIR_NAME + ".alt")).exists()
