"""Regressionstests für die Verteidigung gegen mehrfach "primäre" Pfade desselben
Dokuments in den Such-Ergebnislisten (_search_filename, _search_filtered, _search_like,
_search). upsert_path() verhindert das jetzt beim Schreiben (tests/test_queries.py),
aber bereits vorhandene, "kaputte" Produktionsdaten (vor diesem Fix gescannt) können noch
zwei is_primary=1-Zeilen haben -- die Suche selbst muss auch DANN nur eine Zeile pro
Dokument-ID liefern. Simuliert das direkt per Roh-SQL (umgeht das jetzt fixierte
upsert_path), um genau den vom Nutzer gemeldeten Zustand nachzustellen."""
from db import queries


def _make_doc_with_two_primary_paths(conn, project_id, filename, content):
    """Simuliert bereits kaputte Bestandsdaten: zwei document_paths-Zeilen, BEIDE
    is_primary=1, fuer dasselbe Dokument -- der Zustand, den upsert_path() jetzt beim
    Schreiben verhindert, der aber in existierenden DBs schon vorkommen kann."""
    doc_id = queries.upsert_document(conn, {
        "project_id":  project_id,
        "hash":        f"h-{filename}",
        "filename":    filename,
        "extension":   ".pdf",
        "filesize":    len(content),
        "modified_at": "2026-01-01T00:00:00Z",
        "source_type": "filesystem",
    })
    queries.set_extraction_status(conn, doc_id, "ok")
    conn.execute(
        "INSERT INTO document_paths (document_id, path, is_primary) VALUES (?, ?, 1)",
        (doc_id, f"/scan/{filename}"),
    )
    conn.execute(
        "INSERT INTO document_paths (document_id, path, is_primary) VALUES (?, ?, 1)",
        (doc_id, f"/scan/{filename}-1"),
    )
    queries.upsert_content(conn, doc_id, content)
    queries.save_chunks(conn, doc_id, [{"page_number": None, "chunk_index": 0, "content": content}])
    return doc_id


def test_search_filename_dedupes_despite_two_primary_paths(tmp_db):
    from web.main import _search_filename

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc_with_two_primary_paths(tmp_db, p, "Keller_DA_1-500.pdf", "Haustechnik Plan")
    tmp_db.commit()

    results = _search_filename(tmp_db, "Keller", "", [])
    ids = [r["id"] for r in results]
    assert len(ids) == len(set(ids)), f"Dokument-ID mehrfach in Ergebnissen: {ids}"
    assert len(results) == 1


def test_search_filtered_dedupes_despite_two_primary_paths(tmp_db):
    from web.main import _search_filtered

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc_with_two_primary_paths(tmp_db, p, "brief.pdf", "Sehr geehrte Damen und Herren")
    tmp_db.commit()

    results, error = _search_filtered(tmp_db, "", [])
    assert error is None
    ids = [r["id"] for r in results]
    assert len(ids) == len(set(ids)), f"Dokument-ID mehrfach in Ergebnissen: {ids}"


def test_mcp_search_dedupes_despite_two_primary_paths(tmp_db):
    """End-to-End-Reproduktion des gemeldeten Bugs: gleiche Dokument-ID taucht in
    search() zweimal mit unterschiedlichem Pfad auf ("...DA_1-500-1.pdf" vs.
    "...DA_1-500.pdf")."""
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc_with_two_primary_paths(tmp_db, p, "Keller_Neubau_Haustechnik_DA_1-500.pdf",
                                      "Haustechnik Details Keller Neubau")
    tmp_db.commit()

    c = TestClient(app)
    r = c.get("/api/mcp/search", params={"q": "Keller Neubau Haustechnik"})
    assert r.status_code == 200
    ids = [hit["id"] for hit in r.json()["results"]]
    assert len(ids) == len(set(ids)), f"Dokument-ID mehrfach im MCP-Suchergebnis: {ids}"
