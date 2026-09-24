"""Tests für das MCP-Protokoll (scanner/mcp_log.py) -- jeder Aufruf von
/api/mcp/search und /document muss eine Zeile in mcp_log
hinterlassen, blockierte Treffer mit ihrem Grund."""
import json

from db import queries


def _make_doc(conn, project_id, filename, content, is_norm=0):
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
    if is_norm:
        conn.execute("UPDATE documents SET is_norm = 1 WHERE id = ?", (doc_id,))
        conn.commit()
    return doc_id


def test_mcp_search_writes_log_entry(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    tmp_db.execute("UPDATE projects SET mcp_enabled=1 WHERE id=?", (p,))
    _make_doc(tmp_db, p, "plan.txt", "Grundriss Erdgeschoss mit Wohnflaeche 120qm")
    tmp_db.commit()

    c = TestClient(app)
    r = c.get("/api/mcp/search", params={"q": "Grundriss"})
    assert r.status_code == 200

    rows = tmp_db.execute("SELECT * FROM mcp_log ORDER BY id DESC").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["tool"] == "search"
    assert row["query"] == "Grundriss"
    assert row["status"] == "ok"
    files = json.loads(row["files_json"])
    assert files and files[0]["path"] == "/scan/plan.txt"
    assert row["chars_sent"] > 0
    assert json.loads(row["blocked_json"]) == []


def test_mcp_document_writes_log_entry(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    tmp_db.execute("UPDATE projects SET mcp_enabled=1 WHERE id=?", (p,))
    doc_id = _make_doc(tmp_db, p, "brief.txt", "Sehr geehrte Damen und Herren.")
    tmp_db.commit()

    c = TestClient(app)
    r = c.get("/api/mcp/document", params={"document_id": doc_id})
    assert r.status_code == 200

    row = tmp_db.execute("SELECT * FROM mcp_log WHERE tool='document'").fetchone()
    assert row is not None
    assert row["query"] == str(doc_id)
    assert row["project_id"] == p
    assert row["status"] == "ok"


def test_blocked_norm_hit_appears_in_blocked_json_not_files(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    tmp_db.execute("UPDATE projects SET mcp_enabled=1 WHERE id=?", (p,))
    _make_doc(tmp_db, p, "sia118.txt", "SIA 118 Allgemeine Bedingungen", is_norm=1)
    tmp_db.commit()

    c = TestClient(app)
    r = c.get("/api/mcp/search", params={"q": "SIA 118"})
    assert r.status_code == 200
    data = r.json()
    assert data["results"], "Norm-Treffer soll weiterhin als Treffer erscheinen (nur Inhalt redigiert)"

    row = tmp_db.execute("SELECT * FROM mcp_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "blocked"
    assert json.loads(row["files_json"]) == []
    blocked = json.loads(row["blocked_json"])
    # Der eigentliche redigierte Treffer ...
    assert any(b["reason"] == "Norm erkannt" and "sia118" in (b["path"] or "") for b in blocked)
    # ... UND der Norm-Hinweis (jetzt immer geprüft, auch wenn schon Treffer da sind --
    # siehe Kommentar in web/api.py::mcp_search zur Begründung).
    assert any(b["path"] is None and "SIA 118" in b["reason"] for b in blocked)


def test_blocked_norm_document_read_appears_in_log(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    tmp_db.execute("UPDATE projects SET mcp_enabled=1 WHERE id=?", (p,))
    doc_id = _make_doc(tmp_db, p, "sia380.txt", "SIA 380 Norm-Text", is_norm=1)
    tmp_db.commit()

    c = TestClient(app)
    r = c.get("/api/mcp/document", params={"document_id": doc_id})
    assert r.status_code == 200
    assert r.json()["is_norm"] is True

    row = tmp_db.execute("SELECT * FROM mcp_log WHERE tool='document' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "blocked"
    blocked = json.loads(row["blocked_json"])
    assert blocked[0]["reason"] == "Norm erkannt"


def test_norm_looking_query_with_zero_hits_gets_explicit_notice(tmp_db):
    """Genau das Szenario aus der Praxis: SIA 400 liegt gar nicht (mehr) im Index
    (oder ausserhalb der freigegebenen Projekte) -- die Suche findet nichts, soll
    aber trotzdem klar sagen, dass Normen grundsätzlich nicht über MCP ausgegeben
    werden, statt nur "keine Treffer" zurückzugeben."""
    from fastapi.testclient import TestClient
    from web.main import app

    c = TestClient(app)
    r = c.get("/api/mcp/search", params={"q": "SIA 400"})
    assert r.status_code == 200
    data = r.json()
    assert data["results"] == []
    assert "notice" in data
    assert "SIA 400" in data["notice"]
    assert "Norm" in data["notice"]
    assert "Gefunden unter" not in data["notice"]  # keine Norm im Index -> kein Fundort

    row = tmp_db.execute("SELECT * FROM mcp_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "blocked"
    blocked = json.loads(row["blocked_json"])
    assert any("SIA 400" in b["reason"] for b in blocked)


def test_norm_notice_also_appears_alongside_incidental_matches(tmp_db):
    """Praxisfall SIA 416: die Suche findet oft nur beiläufige ARBEITSDOKUMENTE, die
    die Norm lediglich erwähnen (Berechnungen, Pläne) -- nicht die echte, offiziell
    abgelegte Norm selbst. Der Hinweis mit dem echten Fundort muss auch dann
    erscheinen, wenn die Suche NICHT leer ausgeht (vorher: Hinweis nur bei komplett
    leerem Ergebnis, wodurch die echte Norm bei SIA 416 nie erwähnt wurde)."""
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "Baustelle X", "/scan")
    tmp_db.execute("UPDATE projects SET mcp_enabled=1 WHERE id=?", (p,))
    # Kein is_norm -- nur eine normale Berechnung, die SIA 416 erwähnt.
    _make_doc(tmp_db, p, "Flaechenberechnung_SIA_416.pdf", "Berechnung nach SIA 416")
    tmp_db.commit()

    c = TestClient(app)
    r = c.get("/api/mcp/search", params={"q": "SIA 416"})
    assert r.status_code == 200
    data = r.json()
    assert data["results"], "die beiläufige Berechnung soll weiterhin als Treffer erscheinen"
    assert "notice" in data, "Norm-Hinweis darf nicht fehlen, nur weil auch andere Treffer da sind"
    assert "SIA 416" in data["notice"]


def test_norm_in_non_whitelisted_project_reveals_location_not_content(tmp_db):
    """Kernanforderung: eine Norm in einem Projekt, das NICHT für MCP freigegeben
    ist (oder gar keinem Projekt zugeordnet), soll trotzdem mit Dateiname/Pfad
    auftauchen -- nur der Inhalt bleibt in jedem Fall gesperrt. Ohne diese Ergänzung
    wirkt eine nicht freigegebene Norm identisch zu "existiert überhaupt nicht",
    weil der Whitelist-Filter sie schon vor der Norm-Prüfung aussortiert."""
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "Nicht freigegeben", "/scan")
    # mcp_enabled bewusst NICHT gesetzt (Default 0)
    _make_doc(tmp_db, p, "SIA 400 Plandarstellung.pdf",
              "SIA 400 Volltext, darf nicht rausgegeben werden", is_norm=1)
    tmp_db.commit()

    c = TestClient(app)
    r = c.get("/api/mcp/search", params={"q": "SIA 400"})
    assert r.status_code == 200
    data = r.json()
    assert data["results"] == [], "kein Inhalt darf zurückkommen"
    assert "SIA 400 Plandarstellung.pdf" in data["notice"]
    assert "Nicht freigegeben" in data["notice"]  # Projektname als Fundort-Hinweis
    assert "Volltext" not in data["notice"], "auf keinen Fall der eigentliche Norminhalt"
    # Archivio-Link statt blossem Pfad -- Claude soll den direkt als Markdown-Link
    # ausgeben können, statt selbst (unzuverlässig) einen Link zu konstruieren.
    assert "http://localhost:44380/link?path=" in data["notice"]


def test_normal_query_with_zero_hits_gets_no_notice(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    c = TestClient(app)
    r = c.get("/api/mcp/search", params={"q": "irgendeinbegrifffdenesnichtgibtxyz"})
    assert r.status_code == 200
    assert "notice" not in r.json()


def test_group_mcp_log_entries_groups_by_session_id():
    from web.main import _group_mcp_log_entries

    entries = [
        {"ts": "2026-01-01T10:00:03Z", "session_id": "abc", "status": "ok",
         "files": [{"id": 1}], "blocked": [], "project_name": "P1"},
        {"ts": "2026-01-01T10:00:02Z", "session_id": "abc", "status": "blocked",
         "files": [], "blocked": [{"reason": "x"}], "project_name": "P1"},
        {"ts": "2026-01-01T10:00:01Z", "session_id": "abc", "status": "ok",
         "files": [{"id": 2}, {"id": 3}], "blocked": [], "project_name": "P2"},
        {"ts": "2026-01-01T09:00:00Z", "session_id": "xyz", "status": "ok",
         "files": [{"id": 4}], "blocked": [], "project_name": None},
    ]
    groups = _group_mcp_log_entries(entries)
    assert len(groups) == 2
    g1, g2 = groups
    assert g1["session_id"] == "abc"
    assert g1["count"] == 3
    assert g1["files_count"] == 3
    assert g1["blocked_count"] == 1
    assert g1["status"] == "blocked"  # blocked ueberwiegt ok fuers Gesamt-Badge
    assert g1["projects"] == ["P1", "P2"]
    assert g1["ts_start"] == "2026-01-01T10:00:01Z"
    assert g1["ts_end"] == "2026-01-01T10:00:03Z"
    assert g2["session_id"] == "xyz"
    assert g2["count"] == 1


def test_group_mcp_log_entries_never_merges_across_missing_session_id():
    """Zeilen ohne session_id (NULL) duerfen NIE miteinander verschmolzen werden --
    sonst koennten unabhaengige Alt-/manuelle Zugriffe faelschlich in einer
    gemeinsamen Gruppe landen, nur weil beide session_id=None haben."""
    from web.main import _group_mcp_log_entries

    entries = [
        {"ts": "2026-01-01T10:00:02Z", "session_id": None, "status": "ok",
         "files": [], "blocked": [], "project_name": None},
        {"ts": "2026-01-01T10:00:01Z", "session_id": None, "status": "ok",
         "files": [], "blocked": [], "project_name": None},
    ]
    groups = _group_mcp_log_entries(entries)
    assert len(groups) == 2
    assert all(g["count"] == 1 for g in groups)


def test_mcp_search_sends_session_id_and_groups_in_log(tmp_db):
    """End-zu-End: zwei Aufrufe mit derselben session_id (wie von einem einzigen
    archivio_mcp.py-Subprozess, siehe dessen _SESSION_ID) landen als eine Gruppe."""
    from fastapi.testclient import TestClient
    from web.main import app, _group_mcp_log_entries

    p = queries.insert_project(tmp_db, "P", "/scan")
    tmp_db.execute("UPDATE projects SET mcp_enabled=1 WHERE id=?", (p,))
    _make_doc(tmp_db, p, "plan.txt", "Grundriss Erdgeschoss")
    tmp_db.commit()

    c = TestClient(app)
    c.get("/api/mcp/search", params={"q": "Grundriss", "session_id": "sess-1"})
    c.get("/api/mcp/search", params={"q": "Erdgeschoss", "session_id": "sess-1"})

    rows = tmp_db.execute("SELECT session_id FROM mcp_log ORDER BY id").fetchall()
    assert [r["session_id"] for r in rows] == ["sess-1", "sess-1"]

    r = c.get("/mcp-log")
    assert r.status_code == 200
    assert "2 Zugriffe" in r.text


def test_mcp_log_page_renders_entries(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "Testprojekt", "/scan")
    tmp_db.execute("UPDATE projects SET mcp_enabled=1 WHERE id=?", (p,))
    _make_doc(tmp_db, p, "plan.txt", "Grundriss Erdgeschoss")
    tmp_db.commit()

    c = TestClient(app)
    c.get("/api/mcp/search", params={"q": "Grundriss"})

    r = c.get("/mcp-log")
    assert r.status_code == 200
    assert "plan.txt" in r.text
    assert "Testprojekt" in r.text


def test_mcp_log_csv_export(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    tmp_db.execute("UPDATE projects SET mcp_enabled=1 WHERE id=?", (p,))
    _make_doc(tmp_db, p, "plan.txt", "Grundriss Erdgeschoss")
    tmp_db.commit()

    c = TestClient(app)
    c.get("/api/mcp/search", params={"q": "Grundriss"})

    r = c.get("/mcp-log/export.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "plan.txt" in r.text


def test_mcp_log_is_not_reachable_via_mcp_endpoints(tmp_db):
    """Das Protokoll selbst darf ueber keinen der MCP-Endpunkte abfragbar sein --
    es gibt schlicht keine Route dafuer unter /api/mcp/*."""
    from fastapi.testclient import TestClient
    from web.main import app

    c = TestClient(app)
    for path in ("/api/mcp/log", "/api/mcp/mcp-log", "/api/mcp/mcp_log"):
        r = c.get(path)
        assert r.status_code == 404
