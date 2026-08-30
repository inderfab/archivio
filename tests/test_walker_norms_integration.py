"""Integrationstests: Norm-Klassifikation läuft automatisch während scan_project()
(scanner/walker.py::_classify_norm), Ordner-Lernen nach jedem vollständigen Scan."""
from db import queries
from scanner.walker import scan_project
import scanner.norms as norms_mod


def test_scan_sets_is_norm_from_content(tmp_db, tmp_path, monkeypatch):
    norms_mod._classifier = None
    root = tmp_path / "scan"
    root.mkdir()
    (root / "sia118.txt").write_text(
        "Schweizerischer Ingenieur- und Architektenverein\nSIA 118\nCopyright © SIA"
    )
    (root / "bericht.txt").write_text("Ganz normaler Baubericht ohne Normbezug.")

    project_id = queries.insert_project(tmp_db, "P", str(root))
    tmp_db.commit()
    scan_project(project_id, root)

    rows = {r["filename"]: r["is_norm"] for r in
            tmp_db.execute("SELECT filename, is_norm FROM documents").fetchall()}
    assert rows["sia118.txt"] == 1
    assert rows["bericht.txt"] == 0


def test_rescan_never_touches_manual_override(tmp_db, tmp_path):
    """norm_manual=1 -- der Rescan darf den Wert nie zurücksetzen, sonst geht jede
    manuelle Korrektur beim nächsten Scan verloren."""
    norms_mod._classifier = None
    root = tmp_path / "scan"
    root.mkdir()
    doc = root / "sia118.txt"
    doc.write_text("Schweizerischer Ingenieur- und Architektenverein\nSIA 118\nCopyright © SIA")

    project_id = queries.insert_project(tmp_db, "P", str(root))
    tmp_db.commit()
    scan_project(project_id, root)

    doc_id = tmp_db.execute("SELECT id FROM documents WHERE filename = 'sia118.txt'").fetchone()["id"]
    tmp_db.execute(
        "UPDATE documents SET is_norm = 0, norm_manual = 1, norm_reason = 'manuell freigegeben' WHERE id = ?",
        (doc_id,),
    )
    tmp_db.commit()

    # Datei "aendert" sich nicht, aber ein erneuter Scan laeuft (z.B. taeglich).
    scan_project(project_id, root)

    row = tmp_db.execute(
        "SELECT is_norm, norm_reason FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()
    assert row["is_norm"] == 0
    assert row["norm_reason"] == "manuell freigegeben"


def test_scan_learns_norm_folder_proposal(tmp_db, tmp_path):
    """4 verschiedene Dateien (unterschiedlicher Inhalt -- sonst dedupliziert der
    Content-Hash sie zu EINEM documents-Datensatz mit mehreren document_paths, und
    das Ordner-Lernen zaehlt nur is_primary-Pfade -> faelschlich nur 1 statt 4)."""
    norms_mod._classifier = None
    root = tmp_path / "scan"
    normen = root / "Normen"
    normen.mkdir(parents=True)
    for i in range(4):
        (normen / f"n{i}.txt").write_text(
            f"Schweizerischer Ingenieur- und Architektenverein\nSIA 118\nCopyright © SIA\nDokument {i}"
        )

    project_id = queries.insert_project(tmp_db, "P", str(root))
    tmp_db.commit()
    scan_project(project_id, root)

    row = tmp_db.execute(
        "SELECT status FROM norm_folders WHERE path = ?", (str(normen),)
    ).fetchone()
    assert row is not None
    assert row["status"] == "proposed"
