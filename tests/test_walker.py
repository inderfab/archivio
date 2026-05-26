from db import queries
from scanner.walker import scan_project


def test_scan_indexes_txt(tmp_db, sample_files):
    project_id = queries.insert_project(tmp_db, "Test", str(sample_files))
    tmp_db.commit()

    scan_project(project_id, sample_files)

    rows = tmp_db.execute("SELECT filename, extraction_status FROM documents ORDER BY filename").fetchall()
    names = {r["filename"]: r["extraction_status"] for r in rows}

    assert "plan.txt" in names
    assert "notes.txt" in names
    assert names["plan.txt"] == "ok"
    assert names["notes.txt"] == "ok"


def test_unsupported_extension_skipped(tmp_db, sample_files):
    project_id = queries.insert_project(tmp_db, "Test", str(sample_files))
    tmp_db.commit()

    scan_project(project_id, sample_files)
    row = tmp_db.execute(
        "SELECT id FROM documents WHERE filename = 'binary.xyz'"
    ).fetchone()
    assert row is None  # nicht in supported_extensions → gar nicht indexiert


def test_excluded_folders_not_indexed(tmp_db, sample_files):
    project_id = queries.insert_project(tmp_db, "Test", str(sample_files))
    tmp_db.commit()

    scan_project(project_id, sample_files)

    # Dateien aus Planstande / Upload / Archiv dürfen nicht in der DB sein
    rows = tmp_db.execute(
        "SELECT dp.path FROM document_paths dp"
    ).fetchall()
    paths = [r["path"] for r in rows]

    for excluded in ("Planstande", "Upload", "Archiv"):
        assert not any(excluded in p for p in paths), \
            f"Datei aus Ordner '{excluded}' wurde fälschlicherweise indexiert"


def test_duplicate_file_same_hash(tmp_db, sample_files):
    import shutil
    copy = sample_files / "subdir"
    copy.mkdir()
    shutil.copy(sample_files / "plan.txt", copy / "plan.txt")

    project_id = queries.insert_project(tmp_db, "Test", str(sample_files))
    tmp_db.commit()
    scan_project(project_id, sample_files)

    count = tmp_db.execute(
        "SELECT COUNT(*) FROM documents WHERE filename='plan.txt'"
    ).fetchone()[0]
    paths = tmp_db.execute(
        "SELECT COUNT(*) FROM document_paths dp "
        "JOIN documents d ON d.id = dp.document_id WHERE d.filename='plan.txt'"
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
