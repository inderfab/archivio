from db import queries


def _add_mail(conn, project_id, mailbox, hash_, filename):
    did = conn.execute(
        "INSERT INTO documents (project_id, hash, filename, source_type, extraction_status) "
        "VALUES (?, ?, ?, 'email', 'ok')",
        (project_id, hash_, filename),
    ).lastrowid
    conn.execute(
        "INSERT INTO mails (document_id, mailbox_name) VALUES (?, ?)", (did, mailbox)
    )
    return did


def test_delete_project_removes_reassigned_mailbox_mails(tmp_db):
    """Postfach an Projekt B verknüpft, aber Mails haben noch project_id von A
    (Postfach wurde umgehängt). Beim Löschen von B müssen die Mails trotzdem weg —
    Verknüpfung über mailbox_name, nicht nur project_id."""
    from web import dashboard

    a = queries.insert_project(tmp_db, "A", "/a")
    b = queries.insert_project(tmp_db, "B", "/b")
    tmp_db.execute(
        "INSERT INTO mail_scan_config (mailbox_name, project_id, active) VALUES ('MB', ?, 1)",
        (b,),
    )
    for i in range(3):
        _add_mail(tmp_db, a, "MB", f"h{i}", f"m{i}.eml")  # project_id = A (alt)
    tmp_db.commit()

    dashboard._deletions.clear()
    dashboard._delete_project_bg(b)

    assert tmp_db.execute("SELECT COUNT(*) FROM mails").fetchone()[0] == 0
    assert tmp_db.execute(
        "SELECT COUNT(*) FROM documents WHERE source_type='email'"
    ).fetchone()[0] == 0
    # FTS ebenfalls bereinigt (Trigger 008)
    assert tmp_db.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0] == 0


def test_mailbox_delete_removes_only_its_mails(tmp_db):
    """/mail/delete löscht nur die Mails des Postfachs, andere bleiben."""
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/p")
    tmp_db.execute(
        "INSERT INTO mail_scan_config (mailbox_name, project_id, active, mail_count) "
        "VALUES ('MB1', ?, 1, 2), ('MB2', ?, 1, 1)", (p, p),
    )
    _add_mail(tmp_db, p, "MB1", "a1", "a1.eml")
    _add_mail(tmp_db, p, "MB1", "a2", "a2.eml")
    _add_mail(tmp_db, p, "MB2", "b1", "b1.eml")
    tmp_db.commit()

    c = TestClient(app)
    r = c.post("/dashboard/mail/delete", data={"mailbox_name": "MB1", "context": "mail"})
    assert r.status_code == 200

    # MB1 weg, MB2 bleibt
    assert tmp_db.execute(
        "SELECT COUNT(*) FROM mails WHERE mailbox_name='MB1'"
    ).fetchone()[0] == 0
    assert tmp_db.execute(
        "SELECT COUNT(*) FROM mails WHERE mailbox_name='MB2'"
    ).fetchone()[0] == 1
    row = tmp_db.execute(
        "SELECT mail_count, active FROM mail_scan_config WHERE mailbox_name='MB1'"
    ).fetchone()
    assert row["mail_count"] == 0 and row["active"] == 0


def test_mailbox_toggle_deactivate_shows_confirm(tmp_db):
    """Deaktivieren eines aktiven Postfachs liefert den Lösch-Dialog."""
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/p")
    tmp_db.execute(
        "INSERT INTO mail_scan_config (mailbox_name, project_id, active, mail_count) "
        "VALUES ('MBX', ?, 1, 5)", (p,),
    )
    tmp_db.commit()

    c = TestClient(app)
    r = c.post("/dashboard/mail/toggle", data={"mailbox_name": "MBX", "context": "mail"})
    assert r.status_code == 200
    assert "Mails aus DB entfernen" in r.text
    assert "Nur deaktivieren" in r.text
