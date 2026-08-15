"""Tests für _prefer_project_path() (web/main.py): korrigiert die Anzeige, wenn ein
Projektfilter aktiv ist und ein gefundenes Dokument (per Hash-Dedup) mehrere Pfade in
verschiedenen Projekten hat -- z.B. eine Vorlage, die in mehrere Projektordner kopiert
wurde. Ohne den Fix zeigt die Trefferliste immer den "primären" Pfad/Projektnamen an,
der aus einem GANZ ANDEREN Projekt stammen kann, obwohl der Filter zurecht matcht (eine
Kopie liegt wirklich im gefilterten Projekt) -- wirkt dann wie ein Filter-Leck."""
from db import queries


def _make_duplicated_doc(conn, other_project_id, filtered_project_id, filename, content):
    """Ein Dokument mit ZWEI Pfaden: primär im "anderen" Projekt, zusätzlich eine Kopie
    im gefilterten Projekt -- genau das im Bug-Report beschriebene Szenario."""
    doc_id = queries.upsert_document(conn, {
        "project_id":  other_project_id,
        "hash":        f"h-{filename}",
        "filename":    filename,
        "extension":   ".pdf",
        "filesize":    len(content),
        "modified_at": "2026-01-01T00:00:00Z",
        "source_type": "filesystem",
    })
    queries.set_extraction_status(conn, doc_id, "ok")
    queries.upsert_path(conn, doc_id, f"/scan/other/{filename}", True)   # primär, ANDERES Projekt
    queries.upsert_path(conn, doc_id, f"/scan/keller/{filename}", False)  # Kopie im Zielprojekt
    queries.upsert_content(conn, doc_id, content)
    queries.save_chunks(conn, doc_id, [{"page_number": None, "chunk_index": 0, "content": content}])
    return doc_id


def test_search_shows_path_inside_filtered_project(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    other   = queries.insert_project(tmp_db, "000 Archivprojekte", "/scan/other")
    keller  = queries.insert_project(tmp_db, "200 Keller Winterthur", "/scan/keller")
    _make_duplicated_doc(tmp_db, other, keller, "vorlage.pdf", "Fensterdetail als Vorlage")
    tmp_db.commit()

    c = TestClient(app)
    r = c.get("/search", params={"q": "Fensterdetail", "project_id": keller})
    assert r.status_code == 200
    # search_results.html rendert filepath -- muss den Keller-Pfad zeigen, nicht /scan/other/...
    assert "/scan/keller/vorlage.pdf" in r.text
    assert "/scan/other/vorlage.pdf" not in r.text
    assert "200 Keller Winterthur" in r.text


def test_search_without_project_filter_shows_primary_path(tmp_db):
    """Ohne aktiven Projektfilter bleibt der primäre Pfad unverändert (kein Grund, ihn
    umzubiegen -- es gibt keinen "Filter", auf den korrigiert werden müsste)."""
    from fastapi.testclient import TestClient
    from web.main import app

    other  = queries.insert_project(tmp_db, "000 Archivprojekte", "/scan/other")
    keller = queries.insert_project(tmp_db, "200 Keller Winterthur", "/scan/keller")
    _make_duplicated_doc(tmp_db, other, keller, "vorlage2.pdf", "Fensterdetail als Vorlage")
    tmp_db.commit()

    c = TestClient(app)
    r = c.get("/search", params={"q": "Fensterdetail"})
    assert r.status_code == 200
    assert "/scan/other/vorlage2.pdf" in r.text


def test_mcp_search_shows_path_inside_filtered_project(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    other  = queries.insert_project(tmp_db, "000 Archivprojekte", "/scan/other")
    keller = queries.insert_project(tmp_db, "200 Keller Winterthur", "/scan/keller")
    _make_duplicated_doc(tmp_db, other, keller, "vorlage3.pdf", "Fensterdetail als Vorlage")
    tmp_db.commit()

    c = TestClient(app)
    r = c.get("/api/mcp/search", params={"q": "Fensterdetail", "project_id": keller})
    assert r.status_code == 200
    data = r.json()
    assert data["results"], "erwartete mindestens einen Treffer"
    hit = data["results"][0]
    assert hit["filepath"] == "/scan/keller/vorlage3.pdf"
    assert hit["project_name"] == "200 Keller Winterthur"
