"""Tests für das Aufräumen gelöschter Dateien nach einem VOLLSTÄNDIGEN Scan
(scanner.walker._cleanup_missing_files). Absichtlich vorsichtig: nur nach einem
Scan, der den gesamten Ordnerbaum ohne Cancel/Stall-Abbruch durchlaufen hat --
sonst könnte ein zeitweise nicht erreichbares NAS fälschlich als "Datei gelöscht"
interpretiert werden und Tags/Bewertungen unwiederbringlich verlieren."""
from db import queries
from scanner.walker import scan_project


def test_deleted_file_removed_after_full_rescan(tmp_db, tmp_path):
    root = tmp_path / "scan"
    root.mkdir()
    keep = root / "keep.txt"
    gone = root / "plan xx(1).txt"
    keep.write_text("Grundriss")
    gone.write_text("Duplikat")

    project_id = queries.insert_project(tmp_db, "P", str(root))
    tmp_db.commit()
    scan_project(project_id, root)
    assert tmp_db.execute(
        "SELECT COUNT(*) FROM documents WHERE filename LIKE '%xx(1)%'"
    ).fetchone()[0] == 1

    gone.unlink()
    scan_project(project_id, root)

    assert tmp_db.execute(
        "SELECT COUNT(*) FROM documents WHERE filename LIKE '%xx(1)%'"
    ).fetchone()[0] == 0
    # die verbliebene Datei ist unberuehrt
    assert tmp_db.execute(
        "SELECT COUNT(*) FROM documents WHERE filename = 'keep.txt'"
    ).fetchone()[0] == 1


def test_cleanup_removes_tags_and_ratings_with_last_path(tmp_db, tmp_path):
    """Wenn die letzte Kopie einer Datei verschwindet, muss das ganze Dokument
    (inkl. Tags/Bewertungen per CASCADE) verschwinden -- kein verwaister Eintrag,
    der als graue Kachel in der Galerie haengen bleibt."""
    root = tmp_path / "scan"
    root.mkdir()
    photo = root / "foto.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-content")

    project_id = queries.insert_project(tmp_db, "P", str(root))
    tmp_db.commit()
    scan_project(project_id, root)

    doc_id = tmp_db.execute(
        "SELECT id FROM documents WHERE filename = 'foto.jpg'"
    ).fetchone()["id"]
    tag_id = queries.get_or_create_tag(tmp_db, "Wichtig")
    queries.assign_photo_tag(tmp_db, doc_id, tag_id)
    queries.set_photo_rating(tmp_db, doc_id, 5)
    tmp_db.commit()

    photo.unlink()
    scan_project(project_id, root)

    assert tmp_db.execute("SELECT COUNT(*) FROM documents WHERE id = ?", (doc_id,)).fetchone()[0] == 0
    assert tmp_db.execute("SELECT COUNT(*) FROM photo_tag_assignments WHERE document_id = ?", (doc_id,)).fetchone()[0] == 0
    assert tmp_db.execute("SELECT COUNT(*) FROM photo_ratings WHERE document_id = ?", (doc_id,)).fetchone()[0] == 0


def test_cleanup_keeps_document_when_other_copy_remains(tmp_db, tmp_path):
    """Existiert dieselbe Datei (gleicher Hash) noch an einer zweiten Stelle im
    selben Projekt, darf das Loeschen der einen Kopie das Dokument (und seine
    Tags) NICHT mitreissen -- nur der verwaiste Pfad verschwindet.

    .txt statt .jpg: Bilder bekommen bewusst einen Pfad-basierten Fake-Hash (siehe
    _LIST_ONLY_EXTENSIONS in walker.py, Performance-Optimierung) -- zwei Kopien
    zaehlen dort als zwei GETRENNTE Dokumente, nicht als ein Dokument mit zwei
    Pfaden. Fuer den "gleiche Datei an zwei Orten"-Fall braucht es einen echten
    inhaltsbasierten SHA256-Hash, den nur textextrahierbare Formate bekommen."""
    root = tmp_path / "scan"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir(parents=True)
    content = "identischer Inhalt fuer beide Kopien"
    copy_a = root / "a" / "text.txt"
    copy_b = root / "b" / "text.txt"
    copy_a.write_text(content)
    copy_b.write_text(content)

    project_id = queries.insert_project(tmp_db, "P", str(root))
    tmp_db.commit()
    scan_project(project_id, root)

    doc_id = tmp_db.execute(
        "SELECT id FROM documents WHERE filename = 'text.txt'"
    ).fetchone()["id"]
    assert tmp_db.execute(
        "SELECT COUNT(*) FROM document_paths WHERE document_id = ?", (doc_id,)
    ).fetchone()[0] == 2
    tag_id = queries.get_or_create_tag(tmp_db, "Behalten")
    queries.assign_photo_tag(tmp_db, doc_id, tag_id)
    tmp_db.commit()

    copy_a.unlink()
    scan_project(project_id, root)

    remaining_paths = tmp_db.execute(
        "SELECT path FROM document_paths WHERE document_id = ?", (doc_id,)
    ).fetchall()
    assert [r["path"] for r in remaining_paths] == [str(copy_b)]
    assert tmp_db.execute(
        "SELECT COUNT(*) FROM photo_tag_assignments WHERE document_id = ?", (doc_id,)
    ).fetchone()[0] == 1


def test_cancelled_scan_does_not_clean_up(tmp_db, tmp_path):
    """Ein abgebrochener Scan (Cancel-Flag) darf NIE aufräumen -- sonst könnte ein
    Nutzer, der einen Scan mittendrin abbricht, versehentlich Dokumente verlieren,
    deren Ordner der Scan noch gar nicht erreicht hatte."""
    root = tmp_path / "scan"
    root.mkdir()
    gone = root / "plan.txt"
    gone.write_text("Inhalt")

    project_id = queries.insert_project(tmp_db, "P", str(root))
    tmp_db.commit()
    scan_project(project_id, root)
    assert tmp_db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1

    gone.unlink()
    (root / "neu.txt").write_text("Neue Datei, damit der Scan ueberhaupt etwas zu tun hat")
    scan_project(project_id, root, cancel_flag={"cancel": True})

    # Trotz geloeschter Datei: abgebrochener Scan raeumt nicht auf.
    assert tmp_db.execute(
        "SELECT COUNT(*) FROM documents WHERE filename = 'plan.txt'"
    ).fetchone()[0] == 1
