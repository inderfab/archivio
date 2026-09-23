"""Tests für db/backup.py::import_backup -- Wiederherstellung und Umzug auf einen
anderen Mac.

Zwei Dinge stehen hier im Zentrum:
  * Es wird NIE ungefragt ersetzt und NIE ohne Sicherheitskopie geschrieben.
  * Heisst der Datei-Server auf dem Zielrechner anders, müssen ALLE absoluten Pfade
    umgeschrieben werden. Bleibt auch nur eine Stelle stehen, zeigt der Index dort ins
    Leere und ein späterer Scan löscht die verwaisten Einträge samt Foto-Tags.
"""
import json
import sqlite3

import yaml

from config import settings
from db import backup, connection, queries


def _fill(conn, base="/alt/Projekte"):
    pid = queries.insert_project(conn, "P", f"{base}/P")
    did = queries.upsert_document(conn, {
        "project_id": pid, "hash": "h1", "filename": "plan.pdf", "extension": ".pdf",
        "filesize": 10, "modified_at": None, "source_type": "filesystem"})
    conn.execute("INSERT INTO document_paths (document_id, path, is_primary) VALUES (?,?,1)",
                 (did, f"{base}/P/plan.pdf"))
    conn.execute("INSERT INTO ignored_paths (project_id, path) VALUES (?,?)",
                 (pid, f"{base}/P/temp"))
    conn.execute("INSERT INTO norm_folders (path, status) VALUES (?, 'confirmed')",
                 (f"{base}/Normen",))
    conn.execute("INSERT INTO block_rules (type, value, label) VALUES ('folder', ?, 'Personal')",
                 (f"{base}/Personal",))
    conn.execute("INSERT INTO block_rules (type, value, label) VALUES ('file', ?, 'Lohn')",
                 ("a" * 64,))
    conn.commit()
    return pid


def _make_backup(conn, tmp_path, base=None):
    """Erzeugt eine Sicherung. `base` ist der Basisordner, der im Manifest landet --
    per Voreinstellung ein real existierender, damit der Import nicht schon an der
    Pfadprüfung hängenbleibt. Tests, die genau diese Prüfung meinen, geben einen
    nicht existierenden Pfad mit."""
    if base is None:
        base = str(tmp_path / "projekte")
        (tmp_path / "projekte").mkdir(exist_ok=True)
    settings.save({"scanner": {"base_folders": [{"label": "P", "path": base}]}})
    target = tmp_path / "server"
    target.mkdir(exist_ok=True)
    assert backup.create_backup(target)["ok"] is True
    return target / backup.BACKUP_DIR_NAME


def test_rejects_backup_from_newer_version(tmp_db, tmp_path):
    src = _make_backup(tmp_db, tmp_path)
    manifest = json.loads((src / backup.MANIFEST_FILE).read_text())
    manifest["server_version"] = "999.0.0"
    (src / backup.MANIFEST_FILE).write_text(json.dumps(manifest))

    res = backup.import_backup(src)
    assert res["ok"] is False
    assert "neueren Archivio-Version" in res["error"]


def test_rejects_directory_without_manifest(tmp_db, tmp_path):
    leer = tmp_path / "leer"
    leer.mkdir()
    res = backup.import_backup(leer)
    assert res["ok"] is False
    assert "Keine gueltige Sicherung" in res["error"]


def test_asks_before_replacing_existing_data(tmp_db, tmp_path):
    _fill(tmp_db)
    src = _make_backup(tmp_db, tmp_path)

    res = backup.import_backup(src)

    assert res["ok"] is False
    assert res["needs_confirm"] is True
    assert res["target_stats"]["projects"] == 1
    assert res["backup_stats"]["documents"] == 1


def test_empty_target_needs_no_confirmation(tmp_db, tmp_path):
    src = _make_backup(tmp_db, tmp_path)           # leere DB gesichert
    res = backup.import_backup(src)
    assert res["ok"] is True, res.get("error")
    assert res["stats"]["projects"] == 0


def test_import_creates_safety_copy(tmp_db, tmp_path):
    _fill(tmp_db)
    src = _make_backup(tmp_db, tmp_path)

    res = backup.import_backup(src, confirm_replace=True)

    assert res["ok"] is True, res.get("error")
    safety = res["safety_copy"]
    assert safety and "vor-import-" in safety
    conn = sqlite3.connect(safety)
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
    conn.close()


def test_missing_base_folder_requests_path_mapping(tmp_db, tmp_path):
    _fill(tmp_db)
    src = _make_backup(tmp_db, tmp_path, base="/gibt/es/nicht")

    res = backup.import_backup(src, confirm_replace=True)

    assert res["ok"] is False
    assert res["needs_path_mapping"] == ["/gibt/es/nicht"]


def test_existing_base_folder_needs_no_mapping(tmp_db, tmp_path):
    echt = tmp_path / "echter_ordner"
    echt.mkdir()
    _fill(tmp_db, base=str(echt))
    src = _make_backup(tmp_db, tmp_path, base=str(echt))

    res = backup.import_backup(src, confirm_replace=True)
    assert res["ok"] is True, res.get("error")


def test_path_mapping_rewrites_every_place(tmp_db, tmp_path):
    _fill(tmp_db, base="/alt/Projekte")
    src = _make_backup(tmp_db, tmp_path, base="/alt/Projekte")
    neu = tmp_path / "neuer_server"
    neu.mkdir()

    res = backup.import_backup(src, confirm_replace=True,
                               path_map={"/alt/Projekte": str(neu)})
    assert res["ok"] is True, res.get("error")

    conn = connection.get_connection()
    try:
        assert conn.execute("SELECT path FROM projects").fetchone()[0] == f"{neu}/P"
        assert conn.execute("SELECT path FROM document_paths").fetchone()[0] == f"{neu}/P/plan.pdf"
        assert conn.execute("SELECT path FROM ignored_paths").fetchone()[0] == f"{neu}/P/temp"
        assert conn.execute("SELECT path FROM norm_folders").fetchone()[0] == f"{neu}/Normen"
        folder = conn.execute(
            "SELECT value FROM block_rules WHERE type='folder'").fetchone()[0]
        assert folder == f"{neu}/Personal"
        # Eine Datei-Sperre hält einen SHA256, keinen Pfad -- darf nicht angefasst werden
        datei = conn.execute("SELECT value FROM block_rules WHERE type='file'").fetchone()[0]
        assert datei == "a" * 64
    finally:
        conn.close()

    cfg = settings.load_all()
    assert cfg["scanner"]["base_folders"][0]["path"] == str(neu)
    assert res["rewritten"]["projects.path"] == 1


def test_falls_back_to_project_folders_when_no_base_folders(tmp_db, tmp_path):
    """Ist keine Basisordner-Liste konfiguriert (kommt vor, wenn Projekte einzeln
    angelegt wurden), muss ersatzweise über die Projektordner geprüft werden --
    sonst liefe der Umzug stillschweigend in einen Index, dessen Pfade alle ins
    Leere zeigen."""
    _fill(tmp_db, base="/nirgends/Projekte")
    settings.save({"scanner": {"base_folders": []}})
    target = tmp_path / "server"
    target.mkdir()
    assert backup.create_backup(target)["ok"] is True

    res = backup.import_backup(target / backup.BACKUP_DIR_NAME, confirm_replace=True)

    assert res["ok"] is False
    assert res["needs_path_mapping"] == ["/nirgends/Projekte"]


def test_import_reports_accounts_without_password(tmp_db, tmp_path):
    settings.save({"mail_accounts": [
        {"label": "Büro", "host": "imap.example.com", "username": "u", "password": "geheim"}]})
    src = _make_backup(tmp_db, tmp_path)

    res = backup.import_backup(src, confirm_replace=True)

    assert res["ok"] is True, res.get("error")
    assert res["missing_passwords"] == ["Büro"]
    assert settings.load_all()["mail_accounts"][0]["password"] == ""


def test_database_location_stays_local(tmp_db, tmp_path):
    """Wo die Datenbank liegt, ist eine Eigenschaft des Rechners -- nicht der
    Sicherung. Sonst sucht der Server nach dem Umzug am Pfad des alten Macs."""
    src = _make_backup(tmp_db, tmp_path)
    fremd = yaml.safe_load((src / backup.CONFIG_FILE).read_text())
    fremd["database"] = {"path": "/alter/mac/archivio.db"}
    (src / backup.CONFIG_FILE).write_text(yaml.dump(fremd, allow_unicode=True))

    assert backup.import_backup(src, confirm_replace=True)["ok"] is True
    assert settings.load_all()["database"]["path"] == "archivio.db"
