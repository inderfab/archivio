import re
from datetime import datetime, timedelta, timezone

from web.scan_status import (
    age_label, project_scan_state, scan_state_by_name, state_for_path,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _iso(**delta):
    return (NOW - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_age_label_steps():
    assert age_label(_iso(seconds=10), NOW) == "gerade eben"
    assert age_label(_iso(minutes=5), NOW) == "vor 5 Min."
    assert age_label(_iso(hours=3), NOW) == "vor 3 Std."
    assert age_label(_iso(days=5), NOW) == "vor 5 Tg."
    assert age_label(_iso(days=21), NOW) == "vor 3 Wo."
    assert age_label(_iso(days=90), NOW) == "vor 3 Mon."
    assert age_label(None, NOW) is None


def test_states():
    assert project_scan_state(None, 0, NOW)["state"] == "never"
    fresh = project_scan_state(_iso(hours=5), 0, NOW)
    assert fresh["state"] == "fresh"
    assert re.fullmatch(r"gescannt \d\d\.\d\d\.\d{4} \d\d:\d\d", fresh["label"])
    assert re.search(r"\d\d\.\d\d\.\d{4}", project_scan_state(_iso(days=12), 0, NOW)["label"])
    assert project_scan_state(_iso(days=12), 0, NOW)["state"] == "stale"
    # Archivierte werden bewusst selten gescannt -- nie "veraltet"
    arch = project_scan_state(_iso(days=40), 3, NOW)
    assert arch["state"] == "archived" and "archiviert" in arch["label"]


def test_by_name_and_path(tmp_path, monkeypatch):
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT, path TEXT, "
                 "last_scanned_at TEXT, archive_tier INTEGER DEFAULT 0)")
    conn.execute("INSERT INTO projects VALUES (1,'A','/nas/A',NULL,0)")
    conn.execute("INSERT INTO projects VALUES (2,'A','/nas/B',?,0)", (_iso(minutes=1),))
    conn.execute("INSERT INTO projects VALUES (3,'Sub','/nas/A/Sub',?,0)", (_iso(minutes=1),))
    conn.execute("INSERT INTO projects VALUES (4,'Mail','mailbox:x',NULL,0)")
    # doppelter Name: der schlechtere Stand gewinnt
    assert scan_state_by_name(conn)["A"]["state"] == "never"
    assert "Mail" not in scan_state_by_name(conn)
    # laengster Projektpfad gewinnt
    assert state_for_path(conn, "/nas/A/Sub/x")["name"] == "Sub"
    assert state_for_path(conn, "/nas/A/other")["name"] == "A"
    assert state_for_path(conn, "/nas/AB/x") is None
