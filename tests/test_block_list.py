"""Tests für die manuelle Sperrliste (scanner/block_list.py, block_rules-Tabelle) --
zweiter, unabhängiger Gate-Layer neben der automatischen Norm-Erkennung. Jeder
Regeltyp (file/pattern/folder/project) wird gegen mcp_search/mcp_document geprüft,
dazu der Live-Zähler und dass eine gelöschte Regel sofort wieder Zugriff erlaubt."""
from db import queries
from scanner import block_list


def _make_doc(conn, project_id, filename, content, path=None):
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
    queries.upsert_path(conn, doc_id, path or f"/scan/{filename}", True)
    queries.upsert_content(conn, doc_id, content)
    queries.save_chunks(conn, doc_id, [{"page_number": None, "chunk_index": 0, "content": content}])
    return doc_id


def _enabled_project(conn, name="P", path="/scan"):
    p = queries.insert_project(conn, name, path)
    conn.execute("UPDATE projects SET mcp_enabled=1 WHERE id=?", (p,))
    return p


def _add_rule(conn, type_, value, label=None):
    conn.execute(
        "INSERT INTO block_rules (type, value, label) VALUES (?, ?, ?)",
        (type_, value, label),
    )
    conn.commit()
    block_list.reload_block_rules(conn)


def test_file_rule_blocks_by_hash_not_path(tmp_db):
    """Datei-Regeln matchen über den Hash -- ueberleben also eine Verschiebung/
    Umbenennung, konsistent mit dem projektweiten Hash-Identitaetsprinzip."""
    from fastapi.testclient import TestClient
    from web.main import app

    p = _enabled_project(tmp_db)
    doc_id = _make_doc(tmp_db, p, "lohn_januar.txt", "Vertraulicher Lohninhalt Fabio")
    doc_hash = tmp_db.execute("SELECT hash FROM documents WHERE id=?", (doc_id,)).fetchone()["hash"]
    _add_rule(tmp_db, "file", doc_hash, "Lohn Januar")

    c = TestClient(app)
    r = c.get("/api/mcp/document", params={"document_id": doc_id})
    assert r.status_code == 200
    data = r.json()
    assert data["is_blocked"] is True
    assert "Sperrliste: Lohn Januar" in data["content"]
    assert "Vertraulicher" not in data["content"]


def test_pattern_rule_blocks_matching_filenames(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = _enabled_project(tmp_db)
    blocked_id  = _make_doc(tmp_db, p, "Lohn_2026_Fabio.pdf", "Geheime Lohnzahlen")
    allowed_id  = _make_doc(tmp_db, p, "Grundriss_EG.pdf", "Grundriss Erdgeschoss")
    _add_rule(tmp_db, "pattern", "Lohn_*", "Löhne")

    c = TestClient(app)
    r = c.get("/api/mcp/search", params={"q": "Lohnzahlen"})
    results = r.json()["results"]
    assert results, "Treffer bleibt sichtbar (wie bei Normen) -- nur der Inhalt wird redigiert"
    assert results[0]["filename"] == "Lohn_2026_Fabio.pdf"
    assert "Geheime Lohnzahlen" not in results[0]["excerpt"]
    assert "Sperrliste" in results[0]["excerpt"]

    r2 = c.get("/api/mcp/document", params={"document_id": allowed_id})
    assert "Grundriss" in r2.json()["content"], "nicht betroffene Datei bleibt unberührt"


def test_folder_rule_blocks_documents_under_path(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = _enabled_project(tmp_db)
    blocked_id = _make_doc(tmp_db, p, "vertrag.pdf", "Vertragsinhalt", path="/scan/Verträge/vertrag.pdf")
    other_id   = _make_doc(tmp_db, p, "plan.pdf", "Planinhalt", path="/scan/Pläne/plan.pdf")
    _add_rule(tmp_db, "folder", "/scan/Verträge", "Verträge")

    c = TestClient(app)
    r = c.get("/api/mcp/document", params={"document_id": blocked_id})
    assert r.json()["is_blocked"] is True

    r2 = c.get("/api/mcp/document", params={"document_id": other_id})
    assert "Planinhalt" in r2.json()["content"]


def test_folder_rule_wildcard_matches_any_depth(tmp_db):
    """Ein '*'-Muster im folder-Wert matcht gegen jede Pfadkomponente statt nur
    gegen einen exakten Präfix -- trifft also einen so benannten Ordner
    unabhängig davon, wo genau er im Baum liegt."""
    from fastapi.testclient import TestClient
    from web.main import app

    p = _enabled_project(tmp_db)
    blocked_id = _make_doc(tmp_db, p, "lohn.pdf", "Lohninhalt",
                            path="/scan/Projekt A/Personal/lohn.pdf")
    other_id   = _make_doc(tmp_db, p, "plan.pdf", "Planinhalt",
                            path="/scan/Projekt A/Plaene/plan.pdf")
    _add_rule(tmp_db, "folder", "*Personal*", "Personal (überall)")

    c = TestClient(app)
    r = c.get("/api/mcp/document", params={"document_id": blocked_id})
    assert r.json()["is_blocked"] is True

    r2 = c.get("/api/mcp/document", params={"document_id": other_id})
    assert "Planinhalt" in r2.json()["content"]


def test_project_rule_blocks_whole_project(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = _enabled_project(tmp_db, "Privatprojekt")
    doc_id = _make_doc(tmp_db, p, "notiz.txt", "Private Notiz")
    _add_rule(tmp_db, "project", str(p), "Privatprojekt")

    c = TestClient(app)
    r = c.get("/api/mcp/document", params={"document_id": doc_id})
    assert r.json()["is_blocked"] is True


def test_block_reason_appears_in_mcp_log(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app
    import json as _json

    p = _enabled_project(tmp_db)
    doc_id = _make_doc(tmp_db, p, "geheim.txt", "Geheiminhalt")
    _add_rule(tmp_db, "file",
              tmp_db.execute("SELECT hash FROM documents WHERE id=?", (doc_id,)).fetchone()["hash"],
              "Geheim")

    c = TestClient(app)
    c.get("/api/mcp/document", params={"document_id": doc_id})

    row = tmp_db.execute("SELECT * FROM mcp_log WHERE tool='document' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "blocked"
    blocked = _json.loads(row["blocked_json"])
    assert blocked[0]["reason"] == "Sperrliste: Geheim"


def test_matching_count_per_rule_type(tmp_db):
    p = _enabled_project(tmp_db)
    _make_doc(tmp_db, p, "Lohn_a.pdf", "x")
    _make_doc(tmp_db, p, "Lohn_b.pdf", "x")
    _make_doc(tmp_db, p, "Plan_a.pdf", "x")

    assert block_list.matching_count(tmp_db, "pattern", "Lohn_*") == 2
    assert block_list.matching_count(tmp_db, "project", str(p)) == 3

    doc_id = tmp_db.execute("SELECT id FROM documents WHERE filename='Plan_a.pdf'").fetchone()["id"]
    doc_hash = tmp_db.execute("SELECT hash FROM documents WHERE id=?", (doc_id,)).fetchone()["hash"]
    assert block_list.matching_count(tmp_db, "file", doc_hash) == 1


def test_deleted_rule_immediately_restores_access(tmp_db):
    """Keine Serverneustart-Abhaengigkeit -- reload_block_rules() nach jeder
    Aenderung macht eine geloeschte Regel sofort wirkungslos."""
    from fastapi.testclient import TestClient
    from web.main import app

    p = _enabled_project(tmp_db)
    doc_id = _make_doc(tmp_db, p, "geheim.txt", "Geheiminhalt")
    doc_hash = tmp_db.execute("SELECT hash FROM documents WHERE id=?", (doc_id,)).fetchone()["hash"]
    _add_rule(tmp_db, "file", doc_hash, "Geheim")

    c = TestClient(app)
    r = c.get("/api/mcp/document", params={"document_id": doc_id})
    assert r.json()["is_blocked"] is True

    tmp_db.execute("DELETE FROM block_rules WHERE value=?", (doc_hash,))
    tmp_db.commit()
    block_list.reload_block_rules(tmp_db)

    r2 = c.get("/api/mcp/document", params={"document_id": doc_id})
    assert "Geheiminhalt" in r2.json()["content"]


def test_disabled_rule_has_no_effect(tmp_db):
    p = _enabled_project(tmp_db)
    doc_id = _make_doc(tmp_db, p, "geheim.txt", "Geheiminhalt")
    doc_hash = tmp_db.execute("SELECT hash FROM documents WHERE id=?", (doc_id,)).fetchone()["hash"]
    tmp_db.execute(
        "INSERT INTO block_rules (type, value, label, enabled) VALUES ('file', ?, 'Geheim', 0)",
        (doc_hash,),
    )
    tmp_db.commit()
    block_list.reload_block_rules(tmp_db)

    blocked, reason = block_list.is_blocked(tmp_db, doc_id, "/scan/geheim.txt")
    assert blocked is False
