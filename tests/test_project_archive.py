"""Tests für den Archiv-Status ruhender Projekte (scanner/scan_log.py) -- ein
Projekt, das mehrfach in Folge nichts Neues liefert, wird seltener gescannt. Siehe
Migration 024_project_archive und web/api.py::scan_all()."""
from datetime import datetime, timedelta, timezone

from db import queries
from scanner.scan_log import (
    _ARCHIVE_LADDER_WEEKS,
    _ARCHIVE_QUALIFY_STREAK,
    log_scan,
    toggle_manual_archive,
)


def _scan(conn, project_id, new_count, status="done", n=0):
    log_scan(
        conn, project_id, "P", {"total": 10, "processed": 10, "new": new_count, "skipped": 10 - new_count},
        started_at=f"2026-01-{1+n:02d}T10:00:00Z", finished_at=f"2026-01-{1+n:02d}T10:05:00Z",
        status=status,
    )


def _row(conn, project_id):
    return conn.execute(
        "SELECT archive_tier, archive_streak, archive_next_check_at FROM projects WHERE id=?",
        (project_id,),
    ).fetchone()


def _assert_next_check_in_weeks(iso: str, weeks: int):
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    expected = datetime.now(timezone.utc) + timedelta(weeks=weeks)
    assert abs((dt - expected).total_seconds()) < 60, f"{iso} nicht ~{weeks} Wochen entfernt"


def test_migration_adds_columns_with_defaults(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    row = _row(tmp_db, p)
    assert row["archive_tier"] == 0
    assert row["archive_streak"] == 0
    assert row["archive_next_check_at"] is None


def test_seven_empty_scans_in_a_row_reach_tier_one(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    tmp_db.commit()
    for i in range(_ARCHIVE_QUALIFY_STREAK - 1):
        _scan(tmp_db, p, new_count=0, n=i)
        assert _row(tmp_db, p)["archive_tier"] == 0, "noch nicht qualifiziert"
    _scan(tmp_db, p, new_count=0, n=_ARCHIVE_QUALIFY_STREAK - 1)

    row = _row(tmp_db, p)
    assert row["archive_tier"] == 1
    assert row["archive_streak"] == 0
    _assert_next_check_in_weeks(row["archive_next_check_at"], _ARCHIVE_LADDER_WEEKS[0])


def test_due_check_with_no_new_content_advances_ladder(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    tmp_db.commit()
    for i in range(_ARCHIVE_QUALIFY_STREAK):
        _scan(tmp_db, p, new_count=0, n=i)
    assert _row(tmp_db, p)["archive_tier"] == 1

    _scan(tmp_db, p, new_count=0, n=_ARCHIVE_QUALIFY_STREAK)
    row = _row(tmp_db, p)
    assert row["archive_tier"] == 2
    _assert_next_check_in_weeks(row["archive_next_check_at"], _ARCHIVE_LADDER_WEEKS[1])


def test_new_content_resets_to_tier_zero_from_any_tier(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    tmp_db.commit()
    for i in range(_ARCHIVE_QUALIFY_STREAK):
        _scan(tmp_db, p, new_count=0, n=i)
    assert _row(tmp_db, p)["archive_tier"] == 1

    _scan(tmp_db, p, new_count=2, n=_ARCHIVE_QUALIFY_STREAK)
    row = _row(tmp_db, p)
    assert row["archive_tier"] == 0
    assert row["archive_streak"] == 0
    assert row["archive_next_check_at"] is None


def test_new_content_resets_partial_streak_too(tmp_db):
    """new_count > 0 während der Qualifikationsphase (Stufe 0) muss den Streak
    ebenfalls zurücksetzen, nicht nur eine bereits erreichte Stufe."""
    p = queries.insert_project(tmp_db, "P", "/scan")
    tmp_db.commit()
    _scan(tmp_db, p, new_count=0, n=0)
    _scan(tmp_db, p, new_count=0, n=1)
    assert _row(tmp_db, p)["archive_streak"] == 2

    _scan(tmp_db, p, new_count=1, n=2)
    assert _row(tmp_db, p)["archive_streak"] == 0


def test_error_or_cancelled_scan_does_not_affect_streak(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    tmp_db.commit()
    for i in range(_ARCHIVE_QUALIFY_STREAK - 1):
        _scan(tmp_db, p, new_count=0, n=i)
    streak_before = _row(tmp_db, p)["archive_streak"]

    _scan(tmp_db, p, new_count=0, status="error", n=_ARCHIVE_QUALIFY_STREAK - 1)
    _scan(tmp_db, p, new_count=0, status="cancelled", n=_ARCHIVE_QUALIFY_STREAK)

    row = _row(tmp_db, p)
    assert row["archive_streak"] == streak_before
    assert row["archive_tier"] == 0


def test_scan_all_skips_projects_not_yet_due(tmp_db, monkeypatch):
    """web/api.py::scan_all() ist die gemeinsame Grundlage für den nächtlichen
    Scheduler UND den 'Alle aktiven Projekte jetzt scannen'-Button -- genau hier
    muss die Ersparnis ankommen: ein Projekt mit einer noch in der Zukunft
    liegenden archive_next_check_at wird gar nicht erst gestartet."""
    import asyncio

    import web.api as api

    due    = queries.insert_project(tmp_db, "Faellig", "/scan/faellig")
    future = queries.insert_project(tmp_db, "Nicht faellig", "/scan/nicht-faellig")
    tmp_db.execute("UPDATE projects SET active=1")
    tmp_db.execute(
        "UPDATE projects SET archive_tier=1, archive_next_check_at=? WHERE id=?",
        ("2020-01-01T00:00:00Z", due),
    )
    tmp_db.execute(
        "UPDATE projects SET archive_tier=1, archive_next_check_at=? WHERE id=?",
        ((datetime.now(timezone.utc) + timedelta(weeks=10)).strftime("%Y-%m-%dT%H:%M:%SZ"), future),
    )
    tmp_db.commit()
    api._scans.pop(due, None)
    api._scans.pop(future, None)

    monkeypatch.setattr(api.connection, "get_connection", lambda: tmp_db)

    started_ids = []

    class FakeThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._args = args
        def start(self):
            if self._args:
                started_ids.append(self._args[0])

    monkeypatch.setattr(api.threading, "Thread", FakeThread)

    try:
        asyncio.run(api.scan_all())
        assert due in started_ids
        assert future not in started_ids
    finally:
        # api._scans ist ein modulweiter dict, der Testfälle überdauert (siehe
        # test_scan_all_passes_same_batch_id_to_every_project) -- scan_all() setzt
        # für "due" einen "running"-Eintrag, den ein späterer Test mit derselben
        # (bei 1 beginnenden) project_id sonst fälschlich geerbt hätte.
        api._scans.pop(due, None)
        api._scans.pop(future, None)


def test_manual_toggle_jumps_to_four_week_tier(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    tmp_db.commit()

    now_archived = toggle_manual_archive(tmp_db, p)
    assert now_archived is True
    row = _row(tmp_db, p)
    assert row["archive_tier"] == 3
    assert _ARCHIVE_LADDER_WEEKS[2] == 4
    _assert_next_check_in_weeks(row["archive_next_check_at"], 4)


def test_manual_toggle_off_resets_to_normal(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    tmp_db.commit()
    toggle_manual_archive(tmp_db, p)

    now_archived = toggle_manual_archive(tmp_db, p)
    assert now_archived is False
    row = _row(tmp_db, p)
    assert row["archive_tier"] == 0
    assert row["archive_next_check_at"] is None


def test_dashboard_shows_archived_label_instead_of_freshness(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "Altprojekt", "/scan/alt")
    tmp_db.execute(
        "UPDATE projects SET last_scanned_at=?, archive_tier=2, "
        "archive_next_check_at=? WHERE id=?",
        ("2026-01-01T00:00:00Z",
         (datetime.now(timezone.utc) + timedelta(weeks=2)).strftime("%Y-%m-%dT%H:%M:%SZ"), p),
    )
    tmp_db.commit()

    r = TestClient(app).get("/dashboard")
    assert r.status_code == 200
    assert "archiviert" in r.text
    assert "gescannt vor" not in r.text


def test_nested_project_archives_independently_of_parent(tmp_db):
    """Ein als eigenes Projekt gespeicherter Unterordner hat seine eigene
    project_id und damit einen komplett unabhängigen Archiv-Rhythmus."""
    parent = queries.insert_project(tmp_db, "Elternprojekt", "/scan/eltern")
    child  = queries.insert_project(tmp_db, "Unterordner-Projekt", "/scan/eltern/unter")
    tmp_db.commit()

    for i in range(_ARCHIVE_QUALIFY_STREAK):
        _scan(tmp_db, child, new_count=0, n=i)
    assert _row(tmp_db, child)["archive_tier"] == 1
    assert _row(tmp_db, parent)["archive_tier"] == 0
