"""Tests für scanner/norms_learn.py — Norm-Ordner werden gelernt statt konfiguriert."""
from db import queries
from scanner.norms_learn import learn_norm_folders, confirm_norm_folder, reject_norm_folder


def _make_doc(conn, project_id, folder, filename, is_norm):
    doc_id = queries.upsert_document(conn, {
        "project_id":  project_id,
        "hash":        f"h-{folder}-{filename}",
        "filename":    filename,
        "extension":   ".pdf",
        "filesize":    10,
        "modified_at": "2026-01-01T00:00:00Z",
        "source_type": "filesystem",
    })
    queries.set_extraction_status(conn, doc_id, "ok")
    queries.upsert_path(conn, doc_id, f"{folder}/{filename}", True)
    if is_norm:
        conn.execute("UPDATE documents SET is_norm = 1 WHERE id = ?", (doc_id,))
    conn.commit()
    return doc_id


_CFG = {"folder_learning": {"enabled": True, "min_norm_docs": 3, "min_ratio": 0.6}}


def test_folder_with_high_norm_ratio_proposed(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    for i in range(4):
        _make_doc(tmp_db, p, "/scan/Normen", f"n{i}.pdf", is_norm=1)
    _make_doc(tmp_db, p, "/scan/Normen", "other.pdf", is_norm=0)

    n = learn_norm_folders(tmp_db, _CFG)
    assert n == 1
    row = tmp_db.execute("SELECT status, n_docs, n_norms FROM norm_folders WHERE path = '/scan/Normen'").fetchone()
    assert row["status"] == "proposed"
    assert row["n_docs"] == 5
    assert row["n_norms"] == 4


def test_folder_below_min_docs_not_proposed(tmp_db):
    """Absolute Untergrenze -- 2 Normen reichen nicht, auch bei 100% Anteil."""
    p = queries.insert_project(tmp_db, "P", "/scan")
    for i in range(2):
        _make_doc(tmp_db, p, "/scan/Kleiner", f"n{i}.pdf", is_norm=1)

    learn_norm_folders(tmp_db, _CFG)
    assert tmp_db.execute("SELECT COUNT(*) FROM norm_folders WHERE path = '/scan/Kleiner'").fetchone()[0] == 0


def test_folder_below_min_ratio_not_proposed(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    for i in range(3):
        _make_doc(tmp_db, p, "/scan/Gemischt", f"n{i}.pdf", is_norm=1)
    for i in range(10):
        _make_doc(tmp_db, p, "/scan/Gemischt", f"other{i}.pdf", is_norm=0)

    learn_norm_folders(tmp_db, _CFG)
    assert tmp_db.execute("SELECT COUNT(*) FROM norm_folders WHERE path = '/scan/Gemischt'").fetchone()[0] == 0


def test_confirmed_folder_never_overwritten_by_relearn(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    for i in range(4):
        _make_doc(tmp_db, p, "/scan/Normen", f"n{i}.pdf", is_norm=1)
    learn_norm_folders(tmp_db, _CFG)
    confirm_norm_folder(tmp_db, "/scan/Normen")

    # Erneutes Lernen (z.B. nach dem naechsten Scan) darf den Status nicht zuruecksetzen.
    _make_doc(tmp_db, p, "/scan/Normen", "n99.pdf", is_norm=1)
    learn_norm_folders(tmp_db, _CFG)

    row = tmp_db.execute("SELECT status FROM norm_folders WHERE path = '/scan/Normen'").fetchone()
    assert row["status"] == "confirmed"


def test_rejected_folder_never_overwritten_by_relearn(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    for i in range(4):
        _make_doc(tmp_db, p, "/scan/Kein_Normordner", f"n{i}.pdf", is_norm=1)
    learn_norm_folders(tmp_db, _CFG)
    reject_norm_folder(tmp_db, "/scan/Kein_Normordner")

    learn_norm_folders(tmp_db, _CFG)
    row = tmp_db.execute("SELECT status FROM norm_folders WHERE path = '/scan/Kein_Normordner'").fetchone()
    assert row["status"] == "rejected"


def test_folder_learning_disabled_via_config(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    for i in range(4):
        _make_doc(tmp_db, p, "/scan/Normen", f"n{i}.pdf", is_norm=1)

    n = learn_norm_folders(tmp_db, {"folder_learning": {"enabled": False}})
    assert n == 0
    assert tmp_db.execute("SELECT COUNT(*) FROM norm_folders").fetchone()[0] == 0
