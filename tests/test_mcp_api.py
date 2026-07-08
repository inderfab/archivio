"""Tests für die /api/mcp/*-Endpunkte, die der Archivio-MCP-Server (helper/archivio_mcp.py)
als Tools für Claude Desktop nutzt."""
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
    # Produktion füllt beides: Volltext in document_content, Chunks in document_chunks
    queries.upsert_content(conn, doc_id, content)
    queries.save_chunks(conn, doc_id, [{"page_number": None, "chunk_index": 0, "content": content}])
    return doc_id


def test_mcp_search_returns_clean_json(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc(tmp_db, p, "plan.txt", "Grundriss Erdgeschoss mit Wohnflaeche 120qm")
    tmp_db.commit()

    c = TestClient(app)
    r = c.get("/api/mcp/search", params={"q": "Grundriss"})
    assert r.status_code == 200

    data = r.json()
    assert data["results"], "erwartete mindestens einen Treffer"
    hit = data["results"][0]
    assert hit["filename"] == "plan.txt"
    assert hit["project_name"] == "P"
    assert hit["filepath"] == "/scan/plan.txt"
    # <mark>-Tags aus dem HTML-Highlighting müssen für den MCP-Client entfernt sein
    assert "<mark>" not in hit["excerpt"] and "</mark>" not in hit["excerpt"]


def test_mcp_search_without_query_returns_empty(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    c = TestClient(app)
    r = c.get("/api/mcp/search", params={"q": ""})
    assert r.status_code == 200
    assert r.json() == {"results": [], "folders": []}


def test_mcp_document_returns_full_content(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = _make_doc(tmp_db, p, "brief.txt", "Sehr geehrte Damen und Herren, anbei der Entwurf.")
    tmp_db.commit()

    c = TestClient(app)
    r = c.get("/api/mcp/document", params={"document_id": doc_id})
    assert r.status_code == 200
    d = r.json()
    assert d["filename"] == "brief.txt"
    assert d["source_type"] == "filesystem"
    assert "anbei der Entwurf" in d["content"]
    assert "mail" not in d  # keine Mail → kein mail-Block


def test_mcp_document_includes_mail_metadata(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    did = tmp_db.execute(
        "INSERT INTO documents (project_id, hash, filename, source_type, extraction_status) "
        "VALUES (?, 'mh1', 'Angebot', 'email', 'ok')", (p,),
    ).lastrowid
    tmp_db.execute(
        "INSERT INTO mails (document_id, sender, recipients, subject, date, mailbox_name) "
        "VALUES (?, 'a@x.ch', 'b@y.ch', 'Angebot', '2026-01-02T10:00:00Z', 'INBOX')", (did,),
    )
    queries.upsert_content(tmp_db, did, "Wir freuen uns über Ihre Anfrage.")
    tmp_db.commit()

    c = TestClient(app)
    r = c.get("/api/mcp/document", params={"document_id": did})
    assert r.status_code == 200
    d = r.json()
    assert d["source_type"] == "email"
    assert d["mail"]["sender"] == "a@x.ch"
    assert d["mail"]["subject"] == "Angebot"
    assert "Anfrage" in d["content"]


def test_mcp_document_404_for_unknown_id(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    c = TestClient(app)
    r = c.get("/api/mcp/document", params={"document_id": 987654})
    assert r.status_code == 404


def test_mcp_semantic_search_is_graceful_without_ollama(tmp_db):
    """Ohne (erreichbares) Ollama darf der Endpoint nicht crashen, sondern muss einen
    sauberen Fehlerzustand liefern (wie /search/ai)."""
    from fastapi.testclient import TestClient
    from web.main import app

    c = TestClient(app)
    r = c.get("/api/mcp/semantic-search", params={"q": "Grundriss"})
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data.get("sources"), list)
