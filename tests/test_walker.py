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


def test_unsupported_extension_listed(tmp_db, sample_files):
    project_id = queries.insert_project(tmp_db, "Test", str(sample_files))
    tmp_db.commit()

    scan_project(project_id, sample_files)
    row = tmp_db.execute(
        "SELECT extraction_status FROM documents WHERE filename = 'binary.xyz'"
    ).fetchone()
    # Unbekannte Formate werden per Dateiname indexiert (Status 'listed'),
    # nicht mehr stillschweigend übersprungen.
    assert row is not None
    assert row["extraction_status"] == "listed"
    # und per Dateiname auffindbar
    hit = tmp_db.execute(
        "SELECT rowid FROM documents_fts WHERE documents_fts MATCH 'binary'"
    ).fetchall()
    assert len(hit) == 1


def test_modified_file_repoints_path(tmp_db, sample_files):
    """Ändert sich der Inhalt, entsteht ein neues Dokument (neuer Hash). Der Pfad
    muss umgehängt und die verwaiste alte Version entfernt werden."""
    project_id = queries.insert_project(tmp_db, "Test", str(sample_files))
    tmp_db.commit()
    scan_project(project_id, sample_files)

    plan = sample_files / "plan.txt"
    old_id = tmp_db.execute(
        "SELECT d.id FROM documents d JOIN document_paths dp ON dp.document_id=d.id "
        "WHERE dp.path=?", (str(plan),)
    ).fetchone()["id"]

    # Inhalt ändern → neuer Hash; mtime sicher verändern
    import os, time
    plan.write_text("Komplett neuer Inhalt: Dachstuhl Statik", encoding="utf-8")
    os.utime(plan, (time.time() + 10, time.time() + 10))

    scan_project(project_id, sample_files)

    # Genau ein Dokument für diesen Pfad, und es ist das NEUE
    rows = tmp_db.execute(
        "SELECT d.id FROM documents d JOIN document_paths dp ON dp.document_id=d.id "
        "WHERE dp.path=?", (str(plan),)
    ).fetchall()
    assert len(rows) == 1
    new_id = rows[0]["id"]
    assert new_id != old_id

    # Alte verwaiste Version ist weg (inkl. FTS-Eintrag)
    assert tmp_db.execute("SELECT 1 FROM documents WHERE id=?", (old_id,)).fetchone() is None
    assert tmp_db.execute(
        "SELECT 1 FROM documents_fts WHERE rowid=?", (old_id,)
    ).fetchone() is None

    # Neuer Inhalt ist auffindbar, alter nicht mehr
    assert tmp_db.execute(
        "SELECT 1 FROM documents_fts WHERE documents_fts MATCH 'Dachstuhl'"
    ).fetchone() is not None
    assert tmp_db.execute(
        "SELECT 1 FROM documents_fts WHERE documents_fts MATCH 'Erdgeschoss'"
    ).fetchone() is None


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
