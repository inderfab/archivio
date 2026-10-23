"""Einzel-Scan eines Postfachs: nur dieses Postfach scannen und nur dort «Scannt…» zeigen."""
from db import queries


def _setup(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    for name in ("Projekte/A", "Projekte/B"):
        tmp_db.execute("INSERT INTO mail_scan_config (mailbox_name, active, project_id) VALUES (?, 1, ?)", (name, p))
    tmp_db.commit()
    return p


def _badges(text):
    return text.count(">Scannt…<"), text.count(">Wartet…<")


def test_single_mailbox_scan_marks_only_that_row(tmp_db, monkeypatch):
    from fastapi.testclient import TestClient
    import web.dashboard as d
    from web.main import app

    _setup(tmp_db)
    monkeypatch.setattr(d, "_mail_scan", {"status": "running", "only_mailbox": "Projekte/A",
                                         "current_mailbox": "Projekte/A"})
    r = TestClient(app).get("/dashboard/mail")
    assert _badges(r.text) == (1, 0)


def test_full_mail_scan_shows_running_and_waiting(tmp_db, monkeypatch):
    from fastapi.testclient import TestClient
    import web.dashboard as d
    from web.main import app

    _setup(tmp_db)
    monkeypatch.setattr(d, "_mail_scan", {"status": "running", "current_mailbox": "Projekte/A"})
    r = TestClient(app).get("/dashboard/mail")
    assert _badges(r.text) == (1, 1)


def test_mailbox_project_scan_only_scans_that_mailbox(tmp_db, monkeypatch):
    import web.dashboard as d

    p = queries.insert_project(tmp_db, "Postfach X", "mailbox:Projekte/X")
    tmp_db.commit()
    calls = []

    def fake_mail_scan(only_mailbox=None):
        calls.append(only_mailbox)
        d._mail_scan["status"] = "done"

    monkeypatch.setattr(d, "_mail_scan", {})
    monkeypatch.setattr(d, "_run_mail_scan", fake_mail_scan)
    d._scans[p] = {"status": "running"}
    d._cancel_flags[p] = {"cancel": False}
    d._run_scan(p, "mailbox:Projekte/X")
    assert calls == ["Projekte/X"], "nur das eine Postfach, nicht alle"
    assert d._scans[p]["status"] == "done"
    assert d._scans[p]["count"] == 0
