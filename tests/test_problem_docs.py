"""Tests für die Dokumentenverarbeitung-Übersicht im Dashboard
(web/dashboard.py::_extraction_overview() + die zugehörigen Retry-/Scan-Routen).
tmp_db (siehe conftest.py) konfiguriert scanner.supported_extensions als
['.txt', '.pdf', '.eml', '.xlsx', '.docx', '.rtf']."""
from db import queries


def _make_doc(conn, project_id, filename, ext, status, filesize=1000):
    doc_id = queries.upsert_document(conn, {
        "project_id":  project_id,
        "hash":        f"h-{filename}",
        "filename":    filename,
        "extension":   ext,
        "filesize":    filesize,
        "modified_at": "2026-01-01T00:00:00Z",
        "source_type": "filesystem",
    })
    queries.upsert_path(conn, doc_id, f"/scan/{filename}", True)
    queries.set_extraction_status(conn, doc_id, status)
    return doc_id


def test_extraction_overview_categorizes_unsupported_extension(tmp_db):
    """.jpg ist weder eine konfigurierte supported_extension noch text-tragend --
    landet auch bei status='listed' in der 'unsupported'-Kategorie, nicht bei
    'oversized'."""
    from web.dashboard import _extraction_overview

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc(tmp_db, p, "foto.jpg", ".jpg", "listed")
    tmp_db.commit()

    overview = _extraction_overview(tmp_db)
    assert overview["unsupported_total"] == 1
    assert overview["unsupported_by_ext"] == {".jpg": 1}
    assert overview["oversized_total"] == 0


def test_extraction_overview_folds_literal_unsupported_status_into_unsupported(tmp_db):
    """Manche älteren Scans setzen den DB-Status wörtlich auf 'unsupported' statt
    'listed' (siehe scanner/extractors.py UnsupportedFormat). Für eine nicht
    konfigurierte Endung (.dwg, hier nicht in supported_extensions) muss das genau
    wie 'listed' in die 'unsupported'-Kategorie einsortiert werden, nicht in
    'oversized' oder 'error'."""
    from web.dashboard import _extraction_overview

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc(tmp_db, p, "plan.dwg", ".dwg", "unsupported")
    tmp_db.commit()

    overview = _extraction_overview(tmp_db)
    assert overview["unsupported_total"] == 1
    assert overview["unsupported_by_ext"] == {".dwg": 1}
    assert overview["oversized_total"] == 0
    assert overview["error_total"] == 0


def test_extraction_overview_configured_but_no_real_extractor_is_unsupported(tmp_db, monkeypatch):
    """.dwg kann in config.yaml als supported_extension eingetragen sein (manche
    Büros tun das optimistisch für CAD-Dateien), ohne dass scanner/extractors.py
    überhaupt einen Extraktor dafür hat (_REGISTRY). Das darf NICHT als 'oversized'
    durchgehen (impliziert 'nur zu gross, Knopf hilft') -- ein Klick auf 'Jetzt
    scannen' würde ja doch wieder nur UnsupportedFormat auslösen."""
    from config import settings
    from web.dashboard import _extraction_overview

    monkeypatch.setattr(settings, "_settings", {
        "scanner": {"supported_extensions": [".pdf", ".dwg"]},
    })

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc(tmp_db, p, "plan.dwg", ".dwg", "unsupported")
    tmp_db.commit()

    overview = _extraction_overview(tmp_db)
    assert overview["unsupported_total"] == 1
    assert overview["unsupported_by_ext"] == {".dwg": 1}
    assert overview["oversized_total"] == 0


def test_extraction_overview_categorizes_oversized_supported_extension(tmp_db):
    """.pdf ist eine konfigurierte supported_extension -- bei status='listed'
    kann das nur an der Grössengrenze liegen, nicht am Format selbst."""
    from web.dashboard import _extraction_overview

    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = _make_doc(tmp_db, p, "riesig.pdf", ".pdf", "listed", filesize=600_000_000)
    tmp_db.commit()

    overview = _extraction_overview(tmp_db)
    assert overview["oversized_total"] == 1
    assert overview["oversized_files"][0]["id"] == doc_id
    assert overview["oversized_files"][0]["filename"] == "riesig.pdf"
    assert overview["unsupported_total"] == 0


def test_extraction_overview_error_bucket_only_text_extractable(tmp_db):
    from web.dashboard import _extraction_overview

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc(tmp_db, p, "kaputt.pdf", ".pdf", "error")
    tmp_db.commit()

    overview = _extraction_overview(tmp_db)
    assert overview["error_total"] == 1
    assert overview["error_files"][0]["filename"] == "kaputt.pdf"
    assert "PDF" in overview["error_files"][0]["reason"]


def test_extraction_overview_ok_without_chunks_is_no_text(tmp_db):
    """status='ok' aber keine Chunks -- typischer Fall für gescannte PDFs ohne
    Textebene (oder alte Dokumente, die vor der heutigen OCR-Stufe verarbeitet
    wurden)."""
    from web.dashboard import _extraction_overview

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc(tmp_db, p, "scan.pdf", ".pdf", "ok", filesize=2_000_000)
    tmp_db.commit()

    overview = _extraction_overview(tmp_db)
    assert overview["no_text_total"] == 1
    assert overview["no_text_by_ext"] == {".pdf": 1}
    assert overview["no_text_sample"][0]["filename"] == "scan.pdf"


def test_extraction_overview_ok_with_chunks_not_counted_anywhere(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = _make_doc(tmp_db, p, "gut.pdf", ".pdf", "ok")
    queries.save_chunks(tmp_db, doc_id, [{"page_number": 1, "chunk_index": 0, "content": "Text"}])
    tmp_db.commit()

    from web.dashboard import _extraction_overview
    overview = _extraction_overview(tmp_db)
    assert overview["no_text_total"] == 0
    assert overview["error_total"] == 0
    assert overview["unsupported_total"] == 0
    assert overview["oversized_total"] == 0


def test_extraction_overview_pending_and_missing_embedding_counts(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc(tmp_db, p, "wartet.pdf", ".pdf", "pending")
    doc_id = _make_doc(tmp_db, p, "gut.pdf", ".pdf", "ok")
    queries.save_chunks(tmp_db, doc_id, [{"page_number": 1, "chunk_index": 0, "content": "Text"}])
    tmp_db.commit()

    from web.dashboard import _extraction_overview
    overview = _extraction_overview(tmp_db)
    assert overview["pending_total"] == 1
    assert overview["missing_embedding"] == 1  # der frisch gespeicherte Chunk hat noch kein Embedding


def test_problem_docs_route_renders_all_categories(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc(tmp_db, p, "foto.jpg", ".jpg", "listed")
    _make_doc(tmp_db, p, "riesig.pdf", ".pdf", "listed", filesize=600_000_000)
    _make_doc(tmp_db, p, "kaputt.pdf", ".pdf", "error")
    _make_doc(tmp_db, p, "scan.pdf", ".pdf", "ok", filesize=2_000_000)
    tmp_db.commit()

    c = TestClient(app)
    r = c.get("/dashboard/problem-docs")
    assert r.status_code == 200
    assert "Nicht unterstützte Dateiformate" in r.text
    assert "Zu gross für automatische Extraktion" in r.text
    assert "Fehler bei der Extraktion" in r.text
    assert "Kein Textinhalt gefunden" in r.text
    assert "riesig.pdf" in r.text
    assert "kaputt.pdf" in r.text
    assert "scan.pdf" in r.text
    assert "Jetzt scannen" in r.text


def test_problem_docs_route_shows_empty_state_when_nothing_open(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    c = TestClient(app)
    r = c.get("/dashboard/problem-docs")
    assert r.status_code == 200
    assert "Keine offenen Punkte" in r.text


def test_retry_errors_resets_only_error_status(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    err_id = _make_doc(tmp_db, p, "kaputt.pdf", ".pdf", "error")
    ok_id = _make_doc(tmp_db, p, "scan.pdf", ".pdf", "ok", filesize=2_000_000)
    tmp_db.commit()

    c = TestClient(app)
    r = c.post("/dashboard/retry-errors")
    assert r.status_code == 200
    assert "1 Dokument auf" in r.text

    assert tmp_db.execute(
        "SELECT extraction_status FROM documents WHERE id=?", (err_id,)
    ).fetchone()["extraction_status"] == "pending"
    # ok-ohne-Chunks bleibt unangetastet -- dafür ist retry-no-text zuständig
    assert tmp_db.execute(
        "SELECT extraction_status FROM documents WHERE id=?", (ok_id,)
    ).fetchone()["extraction_status"] == "ok"


def test_retry_no_text_resets_ok_without_chunks_only(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    scan_id = _make_doc(tmp_db, p, "scan.pdf", ".pdf", "ok", filesize=2_000_000)
    good_id = _make_doc(tmp_db, p, "gut.pdf", ".pdf", "ok")
    queries.save_chunks(tmp_db, good_id, [{"page_number": 1, "chunk_index": 0, "content": "Text"}])
    tmp_db.commit()

    c = TestClient(app)
    r = c.post("/dashboard/retry-no-text")
    assert r.status_code == 200
    assert "1 Dokument auf" in r.text
    # Tesseract ist nicht im Bundle -- die UI darf OCR nicht versprechen.
    assert "OCR" not in r.text

    assert tmp_db.execute(
        "SELECT extraction_status FROM documents WHERE id=?", (scan_id,)
    ).fetchone()["extraction_status"] == "pending"
    # ok MIT Chunks bleibt unangetastet
    assert tmp_db.execute(
        "SELECT extraction_status FROM documents WHERE id=?", (good_id,)
    ).fetchone()["extraction_status"] == "ok"


def test_extract_now_runs_extraction_in_background_thread(tmp_db, monkeypatch, tmp_path):
    """extract-now darf die Anfrage nicht blockieren -- Extraktion läuft in einem
    Hintergrund-Thread. Ersetzt threading.Thread durch eine Variante die sofort
    (synchron) ausführt, damit der Test das Ergebnis ohne Sleep/Polling prüfen kann."""
    from fastapi.testclient import TestClient
    import web.dashboard as dash
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    real_file = tmp_path / "gross.pdf"
    real_file.write_text("Text mit genug Inhalt für einen Chunk " * 5, encoding="utf-8")

    doc_id = queries.upsert_document(tmp_db, {
        "project_id": p, "hash": "h-gross", "filename": "gross.pdf",
        "extension": ".pdf", "filesize": 600_000_000,
        "modified_at": "2026-01-01T00:00:00Z", "source_type": "filesystem",
    })
    queries.upsert_path(tmp_db, doc_id, str(real_file), True)
    queries.set_extraction_status(tmp_db, doc_id, "listed")
    tmp_db.commit()

    def fake_extract_and_store(conn, doc_id_, path):
        queries.set_extraction_status(conn, doc_id_, "ok")
        queries.save_chunks(conn, doc_id_, [{"page_number": None, "chunk_index": 0, "content": "Text"}])
        conn.commit()
        return "ok"

    monkeypatch.setattr("scanner.walker._extract_and_store", fake_extract_and_store)

    class SyncThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target, self._args, self._kwargs = target, args, kwargs or {}
        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(dash.threading, "Thread", SyncThread)

    c = TestClient(app)
    r = c.post(f"/dashboard/extract-now/{doc_id}")
    assert r.status_code == 200

    row = tmp_db.execute("SELECT extraction_status FROM documents WHERE id=?", (doc_id,)).fetchone()
    assert row["extraction_status"] == "ok"
    assert "Wird im Hintergrund verarbeitet" in r.text


def test_run_embeddings_now_starts_background_thread(monkeypatch):
    from fastapi.testclient import TestClient
    import web.dashboard as dash
    from web.main import app

    called = []
    monkeypatch.setattr(dash, "_run_post_scan_embedding", lambda: called.append(True))

    class SyncThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target = target
        def start(self):
            self._target()

    monkeypatch.setattr(dash.threading, "Thread", SyncThread)

    c = TestClient(app)
    r = c.post("/dashboard/run-embeddings-now")
    assert r.status_code == 200
    assert called == [True]
