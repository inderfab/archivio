from db import queries
from scanner.walker import scan_project


def test_scan_indexes_txt_and_md(tmp_db, sample_files):
    project_id = queries.insert_project(tmp_db, "Test", str(sample_files))
    tmp_db.commit()

    scan_project(project_id, sample_files)

    rows = tmp_db.execute("SELECT filename, extraction_status FROM documents ORDER BY filename").fetchall()
    names = {r["filename"]: r["extraction_status"] for r in rows}

    assert "plan.txt" in names
    assert "notes.md" in names
    assert names["plan.txt"] == "ok"
    assert names["notes.md"] == "ok"


def test_unsupported_file_gets_status(tmp_db, sample_files):
    project_id = queries.insert_project(tmp_db, "Test", str(sample_files))
    tmp_db.commit()

    # .xyz is not in supported_extensions → won't be indexed at all
    scan_project(project_id, sample_files)
    row = tmp_db.execute(
        "SELECT id FROM documents WHERE filename = 'binary.xyz'"
    ).fetchone()
    assert row is None  # skipped by walker, not in supported list


def test_duplicate_file_same_hash(tmp_db, sample_files):
    import shutil
    copy = sample_files / "subdir"
    copy.mkdir()
    shutil.copy(sample_files / "plan.txt", copy / "plan.txt")

    project_id = queries.insert_project(tmp_db, "Test", str(sample_files))
    tmp_db.commit()
    scan_project(project_id, sample_files)

    count = tmp_db.execute("SELECT COUNT(*) FROM documents WHERE filename='plan.txt'").fetchone()[0]
    paths = tmp_db.execute(
        "SELECT COUNT(*) FROM document_paths dp JOIN documents d ON d.id = dp.document_id WHERE d.filename='plan.txt'"
    ).fetchone()[0]

    assert count == 1   # ein Dokument
    assert paths == 2   # zwei Pfade


def test_fts_finds_content(tmp_db, sample_files):
    project_id = queries.insert_project(tmp_db, "Test", str(sample_files))
    tmp_db.commit()
    scan_project(project_id, sample_files)

    rows = tmp_db.execute(
        "SELECT rowid FROM documents_fts WHERE documents_fts MATCH 'Grundriss'"
    ).fetchall()
    assert len(rows) == 1
