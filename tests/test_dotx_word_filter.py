"""Tests für .dotx (Word-Vorlage) -- soll wie .docx behandelt werden: Inhalt
extrahierbar und im Typ-Filter "Word (DOCX/DOTX)" mit auffindbar."""
from db import queries
from scanner import extractors


def _make_doc(conn, project_id, filename, extension, content):
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


def test_dotx_uses_docx_extractor():
    assert extractors._REGISTRY[".dotx"] is extractors._REGISTRY[".docx"]


def test_type_filter_docx_includes_dotx_files(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_docx = _make_doc(tmp_db, p, "Baubeschrieb.docx", ".docx", "Grundriss Baubeschrieb")
    doc_dotx = _make_doc(tmp_db, p, "Vorlage.dotx", ".dotx", "Grundriss Vorlage")
    _make_doc(tmp_db, p, "Bericht.pdf", ".pdf", "Grundriss Bericht")
    tmp_db.commit()

    c = _client()
    r = c.get("/search", params={"q": "Grundriss", "type": ".docx"})
    assert f'data-id="{doc_docx}"' in r.text
    assert f'data-id="{doc_dotx}"' in r.text
    assert "Bericht.pdf" not in r.text
