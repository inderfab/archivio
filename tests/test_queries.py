"""Tests für db/queries.py -- insbesondere upsert_path()'s Invariante "höchstens ein
is_primary=1-Pfad pro Dokument". Ohne diese Invariante kann ein Dokument mit mehreren
physischen Kopien (identischer Hash, z.B. "_1-500.pdf" und "_1-500-1.pdf") zwei
gleichzeitig primäre Pfade bekommen -- der LEFT JOIN document_paths ... is_primary=1 in
der Suche liefert dann dieselbe Dokument-ID mehrfach mit unterschiedlichem Pfad
(gemeldeter Bug: IDs 5095/5098 tauchten je zweimal auf)."""
from db import queries


def test_upsert_path_demotes_previous_primary(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = queries.upsert_document(tmp_db, {
        "project_id": p, "hash": "h1", "filename": "plan_1-500.pdf",
        "extension": ".pdf", "filesize": 10, "modified_at": "2026-01-01T00:00:00Z",
        "source_type": "filesystem",
    })

    queries.upsert_path(tmp_db, doc_id, "/scan/plan_1-500.pdf", is_primary=True)
    queries.upsert_path(tmp_db, doc_id, "/scan/plan_1-500-1.pdf", is_primary=True)

    rows = tmp_db.execute(
        "SELECT path, is_primary FROM document_paths WHERE document_id=? ORDER BY path",
        (doc_id,),
    ).fetchall()
    primary_paths = [r["path"] for r in rows if r["is_primary"]]
    assert len(primary_paths) == 1, (
        f"erwartete genau einen primaeren Pfad, gefunden: {primary_paths}"
    )
    assert primary_paths == ["/scan/plan_1-500-1.pdf"], "der zuletzt gesetzte Pfad muss primaer sein"
    # der aeltere Pfad bleibt als Duplikat-Referenz erhalten, nur nicht mehr primaer
    assert {r["path"] for r in rows} == {"/scan/plan_1-500.pdf", "/scan/plan_1-500-1.pdf"}


def test_upsert_path_non_primary_does_not_demote(tmp_db):
    """is_primary=False darf den bestehenden primaeren Pfad nicht anfassen."""
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = queries.upsert_document(tmp_db, {
        "project_id": p, "hash": "h2", "filename": "brief.pdf",
        "extension": ".pdf", "filesize": 10, "modified_at": "2026-01-01T00:00:00Z",
        "source_type": "filesystem",
    })
    queries.upsert_path(tmp_db, doc_id, "/scan/brief.pdf", is_primary=True)
    queries.upsert_path(tmp_db, doc_id, "/scan/brief_kopie.pdf", is_primary=False)

    rows = tmp_db.execute(
        "SELECT path, is_primary FROM document_paths WHERE document_id=?", (doc_id,),
    ).fetchall()
    primary_paths = [r["path"] for r in rows if r["is_primary"]]
    assert primary_paths == ["/scan/brief.pdf"]
