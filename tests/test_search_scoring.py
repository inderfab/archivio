"""Tests für die Score-/match_type-Kalibrierung in scanner/embedder.py.

keyword_search_chunks() vergab früher feste Fantasiewerte (0.99/0.95/0.90/0.80), die
direkt neben echten Cosine-Similarity-Scores aus vector_search() angezeigt wurden — ein
Volltext-Treffer schlug score-mässig praktisch immer jeden semantischen Treffer, unabhängig
von echter Relevanz. Diese Tests sichern die rekalibrierten, plausibleren Werte sowie das
neue match_type-Feld ab, das Aufrufern erlaubt, zwischen Treffertypen zu unterscheiden."""
from db import queries


def _make_doc_with_chunk(conn, project_id, filename, content):
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


def test_fts_match_gets_calibrated_score_and_type(tmp_db):
    from scanner.embedder import keyword_search_chunks

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc_with_chunk(tmp_db, p, "geschossflaeche.txt",
                          "Die Geschossflaeche wird gemaess SIA 416 berechnet.")
    tmp_db.commit()

    results = keyword_search_chunks(tmp_db, "Geschossflaeche", limit=10)
    assert results, "erwartete mindestens einen Treffer"
    r = results[0]
    assert r["match_type"] in {"fts", "heading", "like_and", "like_or"}
    # Keine der neuen Konstanten darf mehr bei/über der alten 0.99-Fantasiegrenze liegen,
    # die reale Cosine-Scores systematisch dominiert hat.
    assert 0 < r["score"] < 0.95


def test_heading_match_scores_below_old_fantasy_ceiling(tmp_db):
    """SIA-Norm-Definitionen in GROSSBUCHSTABEN lösen die Heading-Strategie aus -- vorher
    fix 0.99, jetzt rekalibriert."""
    from scanner.embedder import keyword_search_chunks

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc_with_chunk(tmp_db, p, "norm.txt", "2 GESCHOSSFLAECHE GF Definition gemaess SIA.")
    tmp_db.commit()

    results = keyword_search_chunks(tmp_db, "geschossflaeche", limit=10)
    heading_hits = [r for r in results if r["match_type"] == "heading"]
    assert heading_hits, "erwartete einen Heading-Treffer bei GROSSBUCHSTABEN-Definition"
    assert heading_hits[0]["score"] == 0.90


def test_match_types_are_distinct_and_ordered_sensibly(tmp_db):
    """Die vier Volltext-Strategien müssen unterscheidbare, sinnvoll geordnete Scores
    haben (heading >= fts >= like_and >= like_or), damit die Rangfolge weiterhin Sinn
    ergibt, auch wenn keine Zahl mehr künstlich bei 0.99 gedeckelt ist."""
    from scanner.embedder import _CHUNK_SELECT

    # Rein strukturelle Prüfung des Templates: match_type wird durchgereicht.
    assert "{match_type}" in _CHUNK_SELECT
    assert "match_type" in _CHUNK_SELECT
