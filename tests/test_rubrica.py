"""Tests für die Rubrica-Spiegelung (db/rubrica.py) — voller Mailtext inkl. Signatur in
einer separaten DB, während archivio.db weiterhin die signaturbereinigte Version für die
Suche hält."""
from config import settings
from db.rubrica import get_rubrica_connection, save_signature_source


def _record(message_id="m1", raw_text="Hallo\n\nFreundliche Grüsse\nFabio Indergand\n052 214 20 37"):
    return {
        "message_id":   message_id,
        "mail_date":    "2026-01-02T10:00:00+01:00",
        "sender":       "Fabio Indergand <fi@strut.ch>",
        "sender_email": "fi@strut.ch",
        "recipients":   "pk@strut.ch",
        "cc":           "",
        "subject":      "Grundriss Rückmeldung",
        "raw_text":     raw_text,
        "cleaned_text": "Hallo",
        "attachments":  [],
        "thread_id":    "",
        "mailbox":      "INBOX",
    }


def test_disabled_by_default_no_write(tmp_db):
    # tmp_db-Fixture setzt keine rubrica-Sektion -> Default ist False
    ok = save_signature_source(_record(), "Projekt X", "INBOX")
    assert ok is False
    # Bei deaktiviertem Flag wird die DB-Datei nicht mal angelegt
    from db.rubrica import _resolve_rubrica_path
    assert not _resolve_rubrica_path().exists()


def test_enabled_inserts_pending_row(tmp_db):
    settings._settings.setdefault("rubrica", {})["enabled"] = True

    ok = save_signature_source(_record(), "215 Flurhofstrasse St-Gallen", "INBOX")
    assert ok is True

    conn = get_rubrica_connection()
    row = conn.execute("SELECT * FROM signatur_quelle WHERE message_id='m1'").fetchone()
    conn.close()

    assert row is not None
    assert row["status"] == "pending"
    assert row["absender_email"] == "fi@strut.ch"
    assert row["empfaenger"] == "pk@strut.ch"
    assert row["projekt"] == "215 Flurhofstrasse St-Gallen"
    assert row["postfach"] == "INBOX"
    assert "Freundliche Grüsse" in row["text"]


def test_dedup_on_second_call(tmp_db):
    settings._settings.setdefault("rubrica", {})["enabled"] = True

    first  = save_signature_source(_record(), "Projekt X", "INBOX")
    second = save_signature_source(_record(), "Projekt X", "INBOX")
    assert first is True
    assert second is False  # INSERT OR IGNORE -> schon vorhanden

    conn  = get_rubrica_connection()
    count = conn.execute("SELECT COUNT(*) FROM signatur_quelle").fetchone()[0]
    conn.close()
    assert count == 1


def test_save_mail_to_db_mirrors_full_text_while_search_gets_cleaned(tmp_db):
    """Kernpunkt der Änderung: document_content (Suche) bekommt die bereinigte Version,
    signatur_quelle (Rubrica) bekommt den vollen Text inkl. Signatur."""
    from db import queries
    from scanner.mail_scanner import save_mail_to_db

    settings._settings.setdefault("rubrica", {})["enabled"] = True

    project_id = queries.insert_project(tmp_db, "P", "/p")
    tmp_db.commit()

    record = _record(
        message_id="m2",
        raw_text="Sehr geehrte Damen und Herren\n\nFreundliche Grüsse\nFabio Indergand\nStrut Architekten AG",
    )
    ok = save_mail_to_db(tmp_db, record, project_id, "INBOX", "P")
    assert ok is True

    # archivio.db: nur die bereinigte Version (kein Signatur-Rauschen in der Suche)
    doc_id = tmp_db.execute(
        "SELECT id FROM documents WHERE hash='m2'"
    ).fetchone()["id"]
    content = tmp_db.execute(
        "SELECT content FROM document_content WHERE document_id=?", (doc_id,)
    ).fetchone()["content"]
    assert content == "Hallo"
    assert "Freundliche Grüsse" not in content

    # rubrica.db: der volle Text inkl. Signatur
    rconn = get_rubrica_connection()
    row = rconn.execute("SELECT text FROM signatur_quelle WHERE message_id='m2'").fetchone()
    rconn.close()
    assert "Freundliche Grüsse" in row["text"]
    assert "Strut Architekten AG" in row["text"]


def test_settings_toggle_persists_enabled_and_preserves_db_path(tmp_db):
    """UI-Checkbox 'Mails in Datenbank für Rubrica speichern' (Einstellungen) — POST
    /dashboard/settings setzt enabled, lässt einen bereits gesetzten db_path unangetastet
    (settings.save() merged pro Sektion statt zu ersetzen)."""
    from fastapi.testclient import TestClient
    from web.main import app

    settings.save({"rubrica": {"enabled": False, "db_path": "/custom/path/rubrica.db"}})

    c = TestClient(app)
    r = c.post("/dashboard/settings", data={
        "office_name": "Test", "office_language": "de",
        "server_host": "127.0.0.1", "server_port": "8000",
        "num_workers": "1",
        "rubrica_enabled": "1",
    }, follow_redirects=False)
    assert r.status_code == 303

    cfg = settings.load_all()
    assert cfg["rubrica"]["enabled"] is True
    assert cfg["rubrica"]["db_path"] == "/custom/path/rubrica.db"
