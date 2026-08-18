"""Tests für den Tag-Filter in der normalen Dokumentsuche (/search?tag_id=...) --
gleicher Mechanismus wie der Tag-Filter in der Foto-Galerie, aber für alle
Dokumenttypen."""
from db import queries


def _make_doc(conn, project_id, filename, content, extension=".txt"):
    doc_id = queries.upsert_document(conn, {
        "project_id":  project_id,
        "hash":        f"h-{filename}",
        "filename":    filename,
        "extension":   extension,
        "filesize":    len(content),
        "modified_at": "2026-01-01T00:00:00Z",
        "source_type": "filesystem",
    })
    queries.set_extraction_status(conn, doc_id, "ok")
    queries.upsert_path(conn, doc_id, f"/scan/{filename}", True)
    queries.upsert_content(conn, doc_id, content)
    queries.save_chunks(conn, doc_id, [{"page_number": None, "chunk_index": 0, "content": content}])
    return doc_id


def _client():
    from fastapi.testclient import TestClient
    from web.main import app
    return TestClient(app)


def test_search_tag_filter_without_query_returns_tagged_docs(tmp_db):
    """Tag-Filter allein (ohne Suchbegriff) muss bereits Treffer liefern -- tag_id
    gehört zu den 'has_filters'-Kriterien, sonst würde die Suche gar nicht laufen."""
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_a = _make_doc(tmp_db, p, "a.txt", "Bericht Baustelle")
    _make_doc(tmp_db, p, "b.txt", "Anderer Bericht")
    tag_id = queries.get_or_create_tag(tmp_db, "Wichtig")
    queries.assign_photo_tag(tmp_db, doc_a, tag_id)
    tmp_db.commit()

    c = _client()
    r = c.get("/search", params={"tag_id": str(tag_id)})
    assert r.status_code == 200
    assert "a.txt" in r.text
    assert "b.txt" not in r.text


def test_search_tag_filter_combines_with_query(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_a = _make_doc(tmp_db, p, "a.txt", "Grundriss Erdgeschoss")
    doc_b = _make_doc(tmp_db, p, "b.txt", "Grundriss Obergeschoss")
    tag_id = queries.get_or_create_tag(tmp_db, "Wichtig")
    queries.assign_photo_tag(tmp_db, doc_a, tag_id)
    tmp_db.commit()

    c = _client()
    r = c.get("/search", params={"q": "Grundriss", "tag_id": str(tag_id)})
    assert "a.txt" in r.text
    assert "b.txt" not in r.text


def test_search_no_tag_filter_returns_all(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc(tmp_db, p, "a.txt", "Bericht Baustelle")
    _make_doc(tmp_db, p, "b.txt", "Bericht Baustelle Zwei")
    tmp_db.commit()

    c = _client()
    r = c.get("/search", params={"q": "Bericht"})
    assert "a.txt" in r.text
    assert "b.txt" in r.text
