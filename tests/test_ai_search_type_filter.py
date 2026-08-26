"""Regressionstest: Die KI-Suche ignorierte einen in der Such-UI gesetzten Typ-Filter
(z.B. "Mail") komplett -- /search/ai und /search/ai/answer nahmen nur q/project_id
entgegen, keyword_search_chunks()/vector_search() hatten gar keinen Parameter dafür.
Ein Nutzer, der auf "Mail" filtert, bekam trotzdem Treffer aus PDFs etc."""
import numpy as np
from db import queries
from web.main import _ai_type_filter_sql


def _make_doc_with_chunk(conn, project_id, filename, extension, source_type, content):
    doc_id = queries.upsert_document(conn, {
        "project_id":  project_id,
        "hash":        f"h-{filename}",
        "filename":    filename,
        "extension":   extension,
        "filesize":    len(content),
        "modified_at": "2026-01-01T00:00:00Z",
        "source_type": source_type,
    })
    queries.set_extraction_status(conn, doc_id, "ok")
    queries.upsert_path(conn, doc_id, f"/scan/{filename}", True)
    queries.save_chunks(conn, doc_id, [{"page_number": None, "chunk_index": 0, "content": content}])
    conn.commit()
    return doc_id


def test_ai_type_filter_sql_mail():
    sql, params = _ai_type_filter_sql("mail")
    assert "source_type = 'email'" in sql
    assert params == []


def test_ai_type_filter_sql_empty():
    assert _ai_type_filter_sql("") == ("", [])


def test_ai_type_filter_sql_extension():
    sql, params = _ai_type_filter_sql(".pdf")
    assert "d.extension = ?" in sql
    assert params == [".pdf"]


def test_keyword_search_chunks_respects_mail_type_filter(tmp_db):
    """Kernstück des Bugs: Filter 'nur Mail' in der KI-Suche gesetzt -- ein Treffer
    in einem PDF darf dann nicht mehr zurückkommen, auch wenn er textlich besser passt."""
    from scanner.embedder import keyword_search_chunks

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc_with_chunk(tmp_db, p, "vertrag.pdf", ".pdf", "filesystem",
                          "Die Logofarbe wird auf weiss/rot festgelegt gemäss Absprache.")
    _make_doc_with_chunk(tmp_db, p, "mail1.eml", ".eml", "email",
                          "Betreff Logo Farbe: die Möwe/das Logo ist weiss/rot wie gewünscht.")

    type_sql, type_params = _ai_type_filter_sql("mail")
    results = keyword_search_chunks(tmp_db, "logo farbe weiss rot", limit=10,
                                     extra_filter_sql=type_sql, extra_filter_params=type_params)
    assert results, "Mail-Treffer haette gefunden werden muessen"
    assert all(r["filename"] == "mail1.eml" for r in results)


def test_vector_search_respects_mail_type_filter(tmp_db):
    from scanner.embedder import vector_search

    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_pdf  = _make_doc_with_chunk(tmp_db, p, "vertrag.pdf", ".pdf", "filesystem", "Inhalt PDF")
    doc_mail = _make_doc_with_chunk(tmp_db, p, "mail1.eml", ".eml", "email", "Inhalt Mail")

    # Synthetische, identische Embeddings -- Cosine-Similarity ist fuer beide gleich,
    # der Typ-Filter allein entscheidet was zurueckkommt.
    vec = np.ones(8, dtype=np.float32)
    vec = vec / np.linalg.norm(vec)
    for doc_id in (doc_pdf, doc_mail):
        tmp_db.execute(
            "UPDATE document_chunks SET embedding = ? WHERE document_id = ?",
            (vec.tobytes(), doc_id),
        )
    tmp_db.commit()

    type_sql, type_params = _ai_type_filter_sql("mail")
    results = vector_search(tmp_db, vec, limit=10,
                             extra_filter_sql=type_sql, extra_filter_params=type_params)
    assert results
    assert all(r["filename"] == "mail1.eml" for r in results)
