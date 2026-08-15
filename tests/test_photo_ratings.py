"""Tests für die Sternebewertung (photo_ratings, db/queries.py get/set_photo_rating)."""
from db import queries


def _make_doc(conn, project_id, filename="foto.jpg"):
    doc_id = queries.upsert_document(conn, {
        "project_id":  project_id,
        "hash":        f"h-{filename}",
        "filename":    filename,
        "extension":   ".jpg",
        "filesize":    123,
        "modified_at": "2026-01-01T00:00:00Z",
        "source_type": "filesystem",
    })
    queries.set_extraction_status(conn, doc_id, "listed")
    queries.upsert_path(conn, doc_id, f"/scan/{filename}", True)
    conn.commit()
    return doc_id


def test_unrated_photo_has_rating_zero(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = _make_doc(tmp_db, p)
    assert queries.get_photo_rating(tmp_db, doc_id) == 0


def test_set_rating_persists(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = _make_doc(tmp_db, p)
    queries.set_photo_rating(tmp_db, doc_id, 4)
    assert queries.get_photo_rating(tmp_db, doc_id) == 4


def test_set_rating_zero_removes_row(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = _make_doc(tmp_db, p)
    queries.set_photo_rating(tmp_db, doc_id, 3)
    queries.set_photo_rating(tmp_db, doc_id, 0)
    assert queries.get_photo_rating(tmp_db, doc_id) == 0
    row = tmp_db.execute("SELECT * FROM photo_ratings WHERE document_id = ?", (doc_id,)).fetchone()
    assert row is None


def test_set_rating_overwrites_previous(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = _make_doc(tmp_db, p)
    queries.set_photo_rating(tmp_db, doc_id, 2)
    queries.set_photo_rating(tmp_db, doc_id, 5)
    assert queries.get_photo_rating(tmp_db, doc_id) == 5


def test_rating_deleted_when_document_deleted(tmp_db):
    """ON DELETE CASCADE: eine Bewertung darf ein gelöschtes Dokument nicht überleben."""
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = _make_doc(tmp_db, p)
    queries.set_photo_rating(tmp_db, doc_id, 5)
    tmp_db.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    tmp_db.commit()
    row = tmp_db.execute("SELECT * FROM photo_ratings WHERE document_id = ?", (doc_id,)).fetchone()
    assert row is None
