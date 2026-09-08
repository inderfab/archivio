"""Tests für die MCP-Freigabe von Postfächern ohne Projekt (mail_scan_config.
mcp_enabled, Migration 017) -- und dass ein mit einem Projekt verknüpftes
Postfach stattdessen dessen mcp_enabled übernimmt statt ein eigenes zu haben."""
from db import queries


def test_new_mailbox_defaults_to_mcp_disabled(tmp_db):
    tmp_db.execute("INSERT INTO mail_scan_config (mailbox_name, active) VALUES ('INBOX', 1)")
    tmp_db.commit()
    row = tmp_db.execute("SELECT mcp_enabled FROM mail_scan_config WHERE mailbox_name='INBOX'").fetchone()
    assert row["mcp_enabled"] == 0


def test_mail_mcp_toggle_route_flips_flag_for_unassigned_mailbox(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    tmp_db.execute("INSERT INTO mail_scan_config (mailbox_name, active) VALUES ('INBOX', 1)")
    tmp_db.commit()

    c = TestClient(app)
    c.post("/dashboard/mail/mcp-toggle", data={"mailbox_name": "INBOX"})
    row = tmp_db.execute("SELECT mcp_enabled FROM mail_scan_config WHERE mailbox_name='INBOX'").fetchone()
    assert row["mcp_enabled"] == 1

    c.post("/dashboard/mail/mcp-toggle", data={"mailbox_name": "INBOX"})
    row = tmp_db.execute("SELECT mcp_enabled FROM mail_scan_config WHERE mailbox_name='INBOX'").fetchone()
    assert row["mcp_enabled"] == 0


def test_mail_mcp_toggle_route_ignores_project_linked_mailbox(tmp_db):
    """Schutz gegen Fehlbedienung: ein mit einem Projekt verknüpftes Postfach hat
    keinen eigenen Schalter im UI -- die Route weigert sich zusätzlich serverseitig,
    dessen mcp_enabled zu ändern, selbst wenn sie direkt aufgerufen wird."""
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    tmp_db.execute(
        "INSERT INTO mail_scan_config (mailbox_name, active, project_id) VALUES ('INBOX', 1, ?)", (p,)
    )
    tmp_db.commit()

    c = TestClient(app)
    c.post("/dashboard/mail/mcp-toggle", data={"mailbox_name": "INBOX"})
    row = tmp_db.execute("SELECT mcp_enabled FROM mail_scan_config WHERE mailbox_name='INBOX'").fetchone()
    assert row["mcp_enabled"] == 0


def _row_for(html: str, mailbox_name: str) -> str:
    """Isoliert den <div class="project-entry">-Block einer bestimmten Postfach-
    Zeile, damit Assertions nicht versehentlich auf einer anderen Zeile matchen."""
    rows = html.split('<div class="project-entry">')
    for row in rows:
        if f'value="{mailbox_name}"' in row:
            return row
    raise AssertionError(f"Keine Zeile für Postfach {mailbox_name!r} gefunden")


def test_mail_section_shows_toggle_only_for_active_unassigned_mailbox(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "Projekt X", "/scan")
    tmp_db.execute("INSERT INTO mail_scan_config (mailbox_name, active, project_id) VALUES ('Zugeordnet', 1, ?)", (p,))
    tmp_db.execute("INSERT INTO mail_scan_config (mailbox_name, active) VALUES ('Unzugeordnet_aktiv', 1)")
    tmp_db.execute("INSERT INTO mail_scan_config (mailbox_name, active) VALUES ('Unzugeordnet_inaktiv', 0)")
    tmp_db.commit()

    c = TestClient(app)
    r = c.get("/dashboard/mail")
    assert r.status_code == 200
    text = r.text

    # Aktiv + zugeordnet: nur der read-only <span>-Indikator, kein Umschalt-Formular
    zugeordnet_row = _row_for(text, "Zugeordnet")
    assert 'mcp-toggle' in zugeordnet_row
    assert '/dashboard/mail/mcp-toggle' not in zugeordnet_row

    # Aktiv + nicht zugeordnet: echtes Formular zum Umschalten
    unzugeordnet_row = _row_for(text, "Unzugeordnet_aktiv")
    assert '/dashboard/mail/mcp-toggle' in unzugeordnet_row
    assert 'mcp-toggle' in unzugeordnet_row

    # Nicht aktiv: gar kein ☁-Element
    inaktiv_row = _row_for(text, "Unzugeordnet_inaktiv")
    assert 'mcp-toggle' not in inaktiv_row


def test_mcp_allowed_doc_ids_mailbox_branch_logic(tmp_db):
    """documents.project_id ist NOT NULL (siehe Test unten) -- ein Dokument mit
    project_id IS NULL kann über die reguläre Anwendung heute nicht entstehen, weil
    _run_mail_scan() Postfächer ohne Projekt komplett überspringt (web/dashboard.py).
    Die Postfach-Freigabe in _mcp_allowed_doc_ids() ist deshalb aktuell toter Code --
    bewusst so gebaut, damit sie korrekt ist, sobald diese Einschränkung einmal fällt,
    ohne dass die Whitelist-Logik dann nochmals angefasst werden müsste. Getestet wird
    hier daher nur, dass sie mit den heute tatsächlich möglichen Dokumenten (immer mit
    Projekt) korrekt bleibt -- siehe test_scoped_search_on_disabled_project_returns_nothing
    & Co. in test_mcp_whitelist.py für die Projekt-Seite dieser Funktion."""
    from web.api import _mcp_allowed_doc_ids

    p = queries.insert_project(tmp_db, "P", "/scan")
    tmp_db.execute("UPDATE projects SET mcp_enabled=1 WHERE id=?", (p,))
    doc_id = queries.upsert_document(tmp_db, {
        "project_id": p, "hash": "h1", "filename": "mail.eml", "extension": ".eml",
        "filesize": 1, "modified_at": "2026-01-01T00:00:00Z", "source_type": "email",
    })
    tmp_db.commit()
    assert _mcp_allowed_doc_ids(tmp_db, [doc_id]) == {doc_id}


def test_document_project_id_cannot_be_null_via_normal_insert(tmp_db):
    """Dokumentiert die tatsächliche Rahmenbedingung, die den vorigen Test motiviert:
    documents.project_id ist NOT NULL -- der reguläre Mail-Scan überspringt Postfächer
    ohne Projekt komplett (web/dashboard.py::_run_mail_scan), das zu ändern ist
    bewusst nicht Teil dieser Änderung."""
    import sqlite3
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        tmp_db.execute(
            "INSERT INTO documents (project_id, hash, filename, source_type, extraction_status) "
            "VALUES (NULL, 'h2', 'x.eml', 'email', 'ok')"
        )
