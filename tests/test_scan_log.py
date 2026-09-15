"""Tests für das Scan-Protokoll (scanner/scan_log.py, scan_log-Tabelle) --
Startzeit, Dauer, Datei-Zähler, Fehler mit Pfad, Spitzenwerte Speicher/CPU."""
import json
import time

from db import queries
from scanner.scan_log import ResourceSampler, log_scan
from scanner.walker import scan_project


def test_log_scan_writes_row_with_counts(tmp_db):
    p = queries.insert_project(tmp_db, "Testprojekt", "/scan")
    tmp_db.commit()

    progress = {"project_name": "Testprojekt", "total": 10, "processed": 10,
                "new": 3, "skipped": 7, "errors": 0}
    log_scan(tmp_db, p, "Testprojekt", progress,
              started_at="2026-01-01T10:00:00Z", finished_at="2026-01-01T10:02:30Z",
              status="done", peak_memory_mb=123.4, peak_cpu_pct=56.7)

    row = tmp_db.execute("SELECT * FROM scan_log WHERE project_id=?", (p,)).fetchone()
    assert row is not None
    assert row["status"] == "done"
    assert row["total"] == 10
    assert row["new_count"] == 3
    assert row["skipped"] == 7
    assert row["duration_s"] == 150.0
    assert row["peak_memory_mb"] == 123.4
    assert row["peak_cpu_pct"] == 56.7
    assert json.loads(row["errors_json"]) == []


def test_log_scan_pulls_error_files_from_documents(tmp_db):
    """Fehlerdateien mit Pfad kommen aus documents/document_paths (extraction_status
    ='error'), nicht aus einem separaten während des Scans mitgeführten Zähler --
    die DB kennt sie nach dem Scan bereits vollständig."""
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = queries.upsert_document(tmp_db, {
        "project_id": p, "hash": "h1", "filename": "kaputt.pdf",
        "extension": ".pdf", "filesize": 1, "modified_at": "2026-01-01T00:00:00Z",
        "source_type": "filesystem",
    })
    queries.set_extraction_status(tmp_db, doc_id, "error")
    queries.upsert_path(tmp_db, doc_id, "/scan/kaputt.pdf", True)
    tmp_db.commit()

    log_scan(tmp_db, p, "P", {"total": 1, "processed": 1, "new": 0, "skipped": 0, "errors": 1},
              started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:01:00Z",
              status="done")

    row = tmp_db.execute("SELECT * FROM scan_log WHERE project_id=?", (p,)).fetchone()
    errors = json.loads(row["errors_json"])
    assert row["error_count"] == 1
    assert errors == [{"filename": "kaputt.pdf", "path": "/scan/kaputt.pdf"}]


def test_resource_sampler_records_positive_values():
    sampler = ResourceSampler(interval=0.1)
    sampler.start()
    # Etwas CPU-Arbeit erzeugen, damit der Sampler tatsächlich etwas misst.
    end = time.time() + 0.5
    x = 0
    while time.time() < end:
        x += 1
    sampler.stop()
    assert sampler.peak_mb > 0


def test_full_scan_writes_scan_log_via_run_scan(tmp_db, sample_files, monkeypatch):
    """End-to-End: ein echter (kleiner) Scan über _run_scan() muss eine
    scan_log-Zeile hinterlassen, inklusive Status und Dateizähler."""
    import web.dashboard as dash

    p = queries.insert_project(tmp_db, "Testprojekt", str(sample_files))
    tmp_db.commit()

    dash._scans[p] = {
        "status": "running", "phase": "collecting", "project_name": "Testprojekt",
        "project_root": str(sample_files), "total": 0, "processed": 0, "new": 0,
        "skipped": 0, "listed": 0, "errors": 0, "current_file": "", "current_folder": "",
        "started_at": "2026-01-01T00:00:00Z",
    }
    dash._cancel_flags[p] = {"cancel": False}

    dash._run_scan(p, str(sample_files), scan_mail=False)

    row = tmp_db.execute(
        "SELECT * FROM scan_log WHERE project_id=? ORDER BY id DESC LIMIT 1", (p,)
    ).fetchone()
    assert row is not None
    assert row["status"] == "done"
    assert row["total"] > 0
    assert row["project_name"] == "Testprojekt"


def test_system_status_page_renders(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "Testprojekt", "/scan")
    tmp_db.commit()
    log_scan(tmp_db, p, "Testprojekt", {"total": 5, "processed": 5, "new": 2, "skipped": 3, "errors": 0},
              started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:01:00Z",
              status="done", peak_memory_mb=200, peak_cpu_pct=40)

    c = TestClient(app)
    r = c.get("/system-status")
    assert r.status_code == 200
    assert "Testprojekt" in r.text
    assert "200" in r.text
    assert "% RAM" in r.text
    assert "2 neu" in r.text


def test_log_scan_stores_batch_id(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    tmp_db.commit()
    log_scan(tmp_db, p, "P", {"total": 1, "processed": 1, "new": 0, "skipped": 0, "errors": 0},
              started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:01:00Z",
              status="done", batch_id="abc123")

    row = tmp_db.execute("SELECT batch_id FROM scan_log WHERE project_id=?", (p,)).fetchone()
    assert row["batch_id"] == "abc123"


def test_group_scan_log_entries_groups_by_batch_id():
    from web.main import _group_scan_log_entries

    scans = [
        {"batch_id": "b1", "project_name": "P3", "started_at": "2026-01-01T00:02:00Z",
         "finished_at": "2026-01-01T00:03:00Z", "total": 5, "new_count": 2, "error_count": 0,
         "peak_memory_mb": 300, "peak_cpu_pct": 20, "status": "done"},
        {"batch_id": "b1", "project_name": "P2", "started_at": "2026-01-01T00:01:00Z",
         "finished_at": "2026-01-01T00:02:00Z", "total": 3, "new_count": 1, "error_count": 1,
         "peak_memory_mb": 500, "peak_cpu_pct": 50, "status": "error"},
        {"batch_id": "b1", "project_name": "P1", "started_at": "2026-01-01T00:00:00Z",
         "finished_at": "2026-01-01T00:01:00Z", "total": 2, "new_count": 0, "error_count": 0,
         "peak_memory_mb": 100, "peak_cpu_pct": 10, "status": "done"},
        {"batch_id": None, "project_name": "Solo", "started_at": "2026-01-01T00:05:00Z",
         "finished_at": "2026-01-01T00:06:00Z", "total": 1, "new_count": 1, "error_count": 0,
         "peak_memory_mb": 50, "peak_cpu_pct": 5, "status": "done"},
    ]
    groups = _group_scan_log_entries(scans)

    assert len(groups) == 2
    batch_group = next(g for g in groups if g["batch_id"] == "b1")
    solo_group  = next(g for g in groups if g["batch_id"] is None)

    assert batch_group["count"] == 3
    assert batch_group["total_files"] == 10
    assert batch_group["total_new"] == 3
    assert batch_group["total_errors"] == 1
    assert batch_group["ts_start"] == "2026-01-01T00:00:00Z"
    assert batch_group["ts_end"] == "2026-01-01T00:03:00Z"
    assert batch_group["peak_memory_mb"] == 500
    assert batch_group["peak_cpu_pct"] == 50
    assert batch_group["status"] == "error"

    assert solo_group["count"] == 1


def test_group_scan_log_entries_all_done_status_is_done():
    from web.main import _group_scan_log_entries

    scans = [
        {"batch_id": "b1", "project_name": "P1", "started_at": "2026-01-01T00:00:00Z",
         "finished_at": "2026-01-01T00:01:00Z", "total": 1, "new_count": 0, "error_count": 0,
         "peak_memory_mb": 10, "peak_cpu_pct": 5, "status": "done"},
        {"batch_id": "b1", "project_name": "P2", "started_at": "2026-01-01T00:01:00Z",
         "finished_at": "2026-01-01T00:02:00Z", "total": 1, "new_count": 0, "error_count": 0,
         "peak_memory_mb": 10, "peak_cpu_pct": 5, "status": "done"},
    ]
    groups = _group_scan_log_entries(scans)
    assert groups[0]["status"] == "done"


def test_scan_all_passes_same_batch_id_to_every_project(tmp_db, monkeypatch):
    """Ein 'Alle scannen'-Lauf muss allen Projekt-Scans dieselbe batch_id mitgeben,
    sonst können sie im Systemstatus nicht als eine Gruppe erscheinen."""
    import web.api as api
    from db import queries

    p1 = queries.insert_project(tmp_db, "P1", "/scan1")
    p2 = queries.insert_project(tmp_db, "P2", "/scan2")
    tmp_db.execute("UPDATE projects SET active=1")
    tmp_db.commit()
    # _scans/_cancel_flags sind modulweite dicts, die über Testfälle hinweg
    # bestehen bleiben -- da jede tmp_db bei 1 zu zaehlen beginnt, koennten sonst
    # Ueberreste eines fruehreren Tests mit derselben id faelschlich "running" zeigen.
    api._scans.pop(p1, None)
    api._scans.pop(p2, None)

    monkeypatch.setattr(api.connection, "get_connection", lambda: tmp_db)

    captured = []

    class FakeThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target = target
            self._kwargs = kwargs or {}
        def start(self):
            if "batch_id" in self._kwargs:
                captured.append(self._kwargs)

    monkeypatch.setattr(api.threading, "Thread", FakeThread)

    import asyncio
    asyncio.run(api.scan_all())

    assert len(captured) == 2
    batch_ids = {c["batch_id"] for c in captured}
    assert len(batch_ids) == 1
    assert None not in batch_ids
