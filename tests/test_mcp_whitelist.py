"""Tests für die MCP-Whitelist (projects.mcp_enabled): standardmässig hat MCP auf
KEIN Projekt Zugriff, ein Projekt muss im Dashboard explizit freigegeben werden
(siehe web/dashboard.py::toggle_project_mcp). Das ist der Test, "der Vertrauen
kostet, wenn er falsch ist" -- ein nicht freigegebenes Projekt darf über keinen
der drei MCP-Inhalt-Endpunkte erreichbar sein, weder direkt noch mitgemischt in
einer unscoped Suche."""
from db import queries


def _make_doc(conn, project_id, filename, content):
    doc_id = queries.upsert_document(conn, {
        "project_id":   project_id,
        "hash":         f"h-{filename}",
        "filename":     filename,
        "extension":    ".txt",
        "filesize":     len(content),
        "modified_at":  "2026-01-01T00:00:00Z",
        "source_type":  "filesystem",
    })
    queries.set_extraction_status(conn, doc_id, "ok")
    queries.upsert_path(conn, doc_id, f"/scan/{filename}", True)
    queries.upsert_content(conn, doc_id, content)
    queries.save_chunks(conn, doc_id, [{"page_number": None, "chunk_index": 0, "content": content}])
    return doc_id


def _two_projects(conn):
    enabled  = queries.insert_project(conn, "Freigegeben", "/scan/freigegeben")
    disabled = queries.insert_project(conn, "Gesperrt", "/scan/gesperrt")
    conn.execute("UPDATE projects SET mcp_enabled=1 WHERE id=?", (enabled,))
    _make_doc(conn, enabled, "erlaubt.txt", "Fassadenkonzept Nordfassade Backstein")
    _make_doc(conn, disabled, "verboten.txt", "Fassadenkonzept Suedfassade Putz")
    conn.commit()
    return enabled, disabled


def test_new_project_defaults_to_mcp_disabled(tmp_db):
    """Migration 014: DEFAULT 0 -- ein frisch angelegtes Projekt ist nie automatisch
    für Claude freigegeben, weder bei Neuinstallationen noch bei Updates."""
    p = queries.insert_project(tmp_db, "P", "/scan")
    row = tmp_db.execute("SELECT mcp_enabled FROM projects WHERE id=?", (p,)).fetchone()
    assert row["mcp_enabled"] == 0


def test_unscoped_search_excludes_disabled_project(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    enabled, disabled = _two_projects(tmp_db)

    c = TestClient(app)
    r = c.get("/api/mcp/search", params={"q": "Fassadenkonzept"})
    assert r.status_code == 200
    data = r.json()
    filenames = [h["filename"] for h in data["results"]]
    assert "erlaubt.txt" in filenames
    assert "verboten.txt" not in filenames


def test_scoped_search_on_disabled_project_returns_nothing(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    enabled, disabled = _two_projects(tmp_db)

    c = TestClient(app)
    r = c.get("/api/mcp/search", params={"q": "Fassadenkonzept", "project_id": disabled})
    assert r.status_code == 200
    assert r.json()["results"] == []

    row = tmp_db.execute("SELECT * FROM mcp_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "blocked"


def test_scoped_search_on_enabled_project_works(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    enabled, disabled = _two_projects(tmp_db)

    c = TestClient(app)
    r = c.get("/api/mcp/search", params={"q": "Fassadenkonzept", "project_id": enabled})
    assert r.status_code == 200
    assert r.json()["results"], "freigegebenes Projekt soll durchsuchbar bleiben"


def test_scoped_search_by_project_name_resolves_to_real_id(tmp_db):
    """search()/semantic_search() zeigen dem LLM nur den Projekt-NAMEN an, nie die
    interne DB-ID (siehe helper/archivio_mcp.py) -- ein späterer scope-Aufruf schickt
    deshalb den Namen oder eine daraus geratene Zahl, keine echte ID. Vorher wurde
    z.B. project_id="211" (aus "211 Emmenhof Derendingen" geraten) via int() direkt
    als DB-ID benutzt -- passte keine echte Projekt-ID zufällig, blockierte das
    genauso wie eine echte Whitelist-Sperre, obwohl das Projekt freigegeben war."""
    from fastapi.testclient import TestClient
    from web.main import app

    enabled, disabled = _two_projects(tmp_db)
    real_id = enabled
    tmp_db.execute("UPDATE projects SET name='211 Emmenhof Derendingen' WHERE id=?", (real_id,))
    tmp_db.commit()
    assert real_id != 211, "Testannahme verletzt: echte ID darf nicht zufällig 211 sein"

    c = TestClient(app)
    for project_ref in ("211 Emmenhof Derendingen", "Emmenhof", "211"):
        r = c.get("/api/mcp/search", params={"q": "Fassadenkonzept", "project_id": project_ref})
        assert r.status_code == 200
        assert r.json()["results"], f"project_id={project_ref!r} sollte auf das freigegebene Projekt auflösen"


def test_scoped_search_with_unknown_project_ref_is_not_logged_as_blocked(tmp_db):
    """Ein unbekannter/nicht auflösbarer Projekt-Bezug ist ein anderer Fall als eine
    echte Whitelist-Sperre -- beides mit demselben "Nicht für Claude freigegeben"
    zu loggen würde eine falsche Positiv-ID unauffindbar machen (siehe
    _resolve_mcp_project_ref)."""
    from fastapi.testclient import TestClient
    from web.main import app

    _two_projects(tmp_db)

    c = TestClient(app)
    r = c.get("/api/mcp/search", params={"q": "Fassadenkonzept", "project_id": "Nichtexistent"})
    assert r.status_code == 200
    assert r.json()["results"] == []

    row = tmp_db.execute("SELECT * FROM mcp_log ORDER BY id DESC LIMIT 1").fetchone()
    assert "nicht gefunden" in row["blocked_json"]
    assert "Nicht für Claude freigegeben" not in row["blocked_json"]


def test_semantic_search_scoped_on_disabled_project_returns_error(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    enabled, disabled = _two_projects(tmp_db)

    c = TestClient(app)
    r = c.get("/api/mcp/semantic-search", params={"q": "Fassade", "project_id": disabled})
    assert r.status_code == 200
    data = r.json()
    assert data["sources"] == []
    assert "nicht für Claude freigegeben" in data["error"]


def test_document_in_disabled_project_is_refused(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    enabled, disabled = _two_projects(tmp_db)
    doc_id = tmp_db.execute(
        "SELECT id FROM documents WHERE filename='verboten.txt'"
    ).fetchone()["id"]

    c = TestClient(app)
    r = c.get("/api/mcp/document", params={"document_id": doc_id})
    assert r.status_code == 200
    data = r.json()
    assert "nicht für Claude freigegeben" in data["content"]
    assert "Fassadenkonzept" not in data["content"]

    row = tmp_db.execute("SELECT * FROM mcp_log WHERE tool='document' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "blocked"


def test_document_in_enabled_project_returns_content(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    enabled, disabled = _two_projects(tmp_db)
    doc_id = tmp_db.execute(
        "SELECT id FROM documents WHERE filename='erlaubt.txt'"
    ).fetchone()["id"]

    c = TestClient(app)
    r = c.get("/api/mcp/document", params={"document_id": doc_id})
    assert r.status_code == 200
    assert "Fassadenkonzept" in r.json()["content"]


def test_base_folders_excludes_disabled_project_path(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    enabled, disabled = _two_projects(tmp_db)

    c = TestClient(app)
    r = c.get("/api/mcp/base-folders")
    assert r.status_code == 200
    folders = r.json()["folders"]
    assert "/scan/freigegeben" in folders
    assert "/scan/gesperrt" not in folders


def test_toggle_mcp_route_flips_flag(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    tmp_db.commit()

    c = TestClient(app)
    r = c.post(f"/dashboard/projects/{p}/mcp-toggle")
    assert r.status_code == 200
    row = tmp_db.execute("SELECT mcp_enabled FROM projects WHERE id=?", (p,)).fetchone()
    assert row["mcp_enabled"] == 1

    r = c.post(f"/dashboard/projects/{p}/mcp-toggle")
    assert r.status_code == 200
    row = tmp_db.execute("SELECT mcp_enabled FROM projects WHERE id=?", (p,)).fetchone()
    assert row["mcp_enabled"] == 0
