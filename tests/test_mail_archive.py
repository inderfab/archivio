"""Tests für den Archiv-Status von Postfächern (mail_scan_config.archive_tier,
Migration 025) -- gleiches Prinzip wie mcp_enabled (siehe test_mail_mcp_whitelist.py):
ein mit einem Projekt verknüpftes Postfach übernimmt dessen archive_tier statt ein
eigenes zu haben."""
from datetime import datetime, timedelta, timezone

from db import queries
from tests.test_mail_mcp_whitelist import _row_for


def test_new_mailbox_defaults_to_archive_disabled(tmp_db):
    tmp_db.execute("INSERT INTO mail_scan_config (mailbox_name, active) VALUES ('INBOX', 1)")
    tmp_db.commit()
    row = tmp_db.execute("SELECT archive_tier FROM mail_scan_config WHERE mailbox_name='INBOX'").fetchone()
    assert row["archive_tier"] == 0


def test_mail_archive_toggle_route_flips_for_unassigned_mailbox(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    tmp_db.execute("INSERT INTO mail_scan_config (mailbox_name, active) VALUES ('INBOX', 1)")
    tmp_db.commit()

    c = TestClient(app)
    c.post("/dashboard/mail/archive-toggle", data={"mailbox_name": "INBOX"})
    row = tmp_db.execute("SELECT archive_tier FROM mail_scan_config WHERE mailbox_name='INBOX'").fetchone()
    assert row["archive_tier"] == 3  # "4 Wochen", wie beim manuellen Projekt-Schalter

    c.post("/dashboard/mail/archive-toggle", data={"mailbox_name": "INBOX"})
    row = tmp_db.execute("SELECT archive_tier FROM mail_scan_config WHERE mailbox_name='INBOX'").fetchone()
    assert row["archive_tier"] == 0


def test_mail_archive_toggle_route_ignores_project_linked_mailbox(tmp_db):
    """Wie bei mcp-toggle: ein verknüpftes Postfach hat keinen eigenen Schalter im
    UI -- die Route weigert sich zusätzlich serverseitig, direkt aufgerufen."""
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    tmp_db.execute(
        "INSERT INTO mail_scan_config (mailbox_name, active, project_id) VALUES ('INBOX', 1, ?)", (p,)
    )
    tmp_db.commit()

    c = TestClient(app)
    c.post("/dashboard/mail/archive-toggle", data={"mailbox_name": "INBOX"})
    row = tmp_db.execute("SELECT archive_tier FROM mail_scan_config WHERE mailbox_name='INBOX'").fetchone()
    assert row["archive_tier"] == 0


def test_mail_section_shows_archive_toggle_only_for_active_unassigned_mailbox(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "Projekt X", "/scan")
    tmp_db.execute("UPDATE projects SET archive_tier=2 WHERE id=?", (p,))
    tmp_db.execute("INSERT INTO mail_scan_config (mailbox_name, active, project_id) VALUES ('Zugeordnet', 1, ?)", (p,))
    tmp_db.execute("INSERT INTO mail_scan_config (mailbox_name, active) VALUES ('Unzugeordnet_aktiv', 1)")
    tmp_db.execute("INSERT INTO mail_scan_config (mailbox_name, active) VALUES ('Unzugeordnet_inaktiv', 0)")
    tmp_db.commit()

    c = TestClient(app)
    r = c.get("/dashboard/mail")
    assert r.status_code == 200
    text = r.text

    # Aktiv + zugeordnet: nur der read-only <span>-Indikator, übernimmt Projekt-Stufe
    zugeordnet_row = _row_for(text, "Zugeordnet")
    assert 'archive-toggle on' in zugeordnet_row
    assert '/dashboard/mail/archive-toggle' not in zugeordnet_row

    # Aktiv + nicht zugeordnet: echtes Formular zum Umschalten
    unzugeordnet_row = _row_for(text, "Unzugeordnet_aktiv")
    assert '/dashboard/mail/archive-toggle' in unzugeordnet_row
    assert 'archive-toggle' in unzugeordnet_row

    # Nicht aktiv: gar kein 📦-Element
    inaktiv_row = _row_for(text, "Unzugeordnet_inaktiv")
    assert 'archive-toggle' not in inaktiv_row


def test_batch_mail_scan_skips_mailbox_of_archived_project(tmp_db, monkeypatch):
    """Ein 'Aktive Postfächer scannen'-Lauf muss ein verknüpftes Postfach
    überspringen, solange dessen Projekt archiviert und nicht fällig ist --
    gleiches Prinzip wie web/api.py::scan_all() für Dateiordner."""
    import web.dashboard as dash

    due    = queries.insert_project(tmp_db, "Faellig", "/scan/faellig")
    future = queries.insert_project(tmp_db, "Nicht faellig", "/scan/nicht-faellig")
    tmp_db.execute(
        "UPDATE projects SET archive_tier=1, archive_next_check_at=? WHERE id=?",
        ("2020-01-01T00:00:00Z", due),
    )
    tmp_db.execute(
        "UPDATE projects SET archive_tier=1, archive_next_check_at=? WHERE id=?",
        ((datetime.now(timezone.utc) + timedelta(weeks=10)).strftime("%Y-%m-%dT%H:%M:%SZ"), future),
    )
    tmp_db.execute(
        "INSERT INTO mail_scan_config (mailbox_name, active, project_id) VALUES ('Faellig_Box', 1, ?)", (due,)
    )
    tmp_db.execute(
        "INSERT INTO mail_scan_config (mailbox_name, active, project_id) VALUES ('Archiviert_Box', 1, ?)", (future,)
    )
    tmp_db.commit()

    monkeypatch.setattr(dash.connection, "get_connection", lambda: tmp_db)

    scanned = []

    class FakeClient:
        def logout(self):
            pass

    def fake_scan_mailbox(client, mailbox_name, project_id, progress=None):
        scanned.append(mailbox_name)
        return {"new": 0, "skipped": 0, "errors": 0}

    import scanner.mail_scanner as mail_scanner
    monkeypatch.setattr(mail_scanner, "connect_imap", lambda: FakeClient())
    monkeypatch.setattr(mail_scanner, "scan_mailbox", fake_scan_mailbox)

    dash._mail_scan.clear()
    dash._run_mail_scan()

    assert "Faellig_Box" in scanned
    assert "Archiviert_Box" not in scanned
