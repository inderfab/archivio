"""Tests für drei Dashboard-Korrekturen an der Projektliste (web/dashboard.py,
web/templates/_dashboard_projects.html):

1. Tiefer verschachtelte Projekte (via "Unterordner ▸" angelegt) erschienen in der
   Reihenfolge ihrer Aufschaltung (SQLite-rowid ohne ORDER BY), nicht alphanumerisch
   wie die obersten Projekte (die _discovered_projects_for_base() bereits per
   os.scandir()+sorted() ordnet).
2. Die Rückfrage beim Deaktivieren eines Projekts ("Aus DB entfernen?") ersetzte
   die GESAMTE Projektliste durch sich selbst -- bei einer langen Liste landete sie
   dadurch ausserhalb der aktuellen Scroll-Position und blieb unsichtbar.
3. Ein Postfach, das einem Projekt zugeordnet ist, erschien doppelt: einmal unter
   dem Projekt (Dashboard) und einmal in der Mail-Integration."""
from db import queries


def test_active_folder_projects_sorted_alphanumerically(tmp_db):
    """Grundlage der Verschachtelungs-Reihenfolge in _project_groups() -- ohne
    ORDER BY liefert SQLite die rowid-Reihenfolge, also die der Aufschaltung."""
    from web.dashboard import _active_folder_projects

    queries.insert_project(tmp_db, "122 BG Schönheim Urdorf", "/scan/122")
    queries.insert_project(tmp_db, "017 Aepli Herisau", "/scan/017")
    queries.insert_project(tmp_db, "099 Dürrenrain Pfungen", "/scan/099")
    tmp_db.execute("UPDATE projects SET active=1")
    tmp_db.commit()

    names = [r["name"] for r in _active_folder_projects(tmp_db)]
    assert names == ["017 Aepli Herisau", "099 Dürrenrain Pfungen", "122 BG Schönheim Urdorf"]


def test_nested_projects_render_in_alphanumeric_order(tmp_path, tmp_db):
    """End-to-End über _project_groups(): ein per os.scandir() gefundenes
    Elternprojekt bekommt zwei tiefer liegende Unterordner-Projekte (eigene
    project_id, Pfad unterhalb des Elternordners) -- im Dashboard müssen sie in
    Namensreihenfolge erscheinen, nicht in Einfüge-Reihenfolge."""
    from config import settings
    from web.dashboard import _project_groups

    base   = tmp_path / "Projekte"
    parent = base / "200 Sammelordner"
    parent.mkdir(parents=True)
    settings._settings.setdefault("scanner", {})["base_folders"] = [
        {"label": "Projekte", "path": str(base)}
    ]
    queries.insert_project(tmp_db, "200 Sammelordner", str(parent))
    queries.insert_project(tmp_db, "220 Zweites Unterprojekt", str(parent / "220"))
    queries.insert_project(tmp_db, "210 Erstes Unterprojekt", str(parent / "210"))
    tmp_db.execute("UPDATE projects SET active=1")
    tmp_db.commit()

    groups = _project_groups(tmp_db)
    nested = [p for grp in groups for p in grp["projects"] if p["nested"]][0]["nested"]
    assert [p["name"] for p in nested] == ["210 Erstes Unterprojekt", "220 Zweites Unterprojekt"]


def test_deactivate_confirmation_retargets_to_its_own_row(tmp_db):
    """HX-Retarget lenkt die Antwort auf genau diese eine Projektzeile um, statt
    (wie das auslösende Formular es sonst täte) die ganze #project-list zu
    ersetzen -- der Rest der Liste bleibt an Ort und Stelle stehen."""
    from fastapi.testclient import TestClient

    from web.main import app

    pid = queries.insert_project(tmp_db, "P", "/scan/p")
    tmp_db.execute("UPDATE projects SET active=1 WHERE id=?", (pid,))
    tmp_db.commit()

    r = TestClient(app).post(
        "/dashboard/projects/toggle", data={"path": "/scan/p", "name": "P"}
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Retarget") == f"#project-row-{pid}"
    assert "Aus DB entfernen" in r.text


def test_project_row_carries_matching_id(tmp_db):
    """Gegenstück zum HX-Retarget-Ziel: die Zeile muss die vom Server erwartete
    id auch tatsächlich tragen, sonst läuft das Retargeting ins Leere."""
    from fastapi.testclient import TestClient

    from web.main import app

    pid = queries.insert_project(tmp_db, "P", "/scan/p")
    tmp_db.execute("UPDATE projects SET active=1 WHERE id=?", (pid,))
    tmp_db.commit()

    r = TestClient(app).get("/dashboard/projects/list")
    assert f'id="project-row-{pid}"' in r.text


def test_mailbox_assigned_to_project_appears_only_in_mail_section(tmp_db):
    """Vorher stand dasselbe Postfach gleichzeitig unter seinem Projekt
    (_dashboard_projects.html) UND in der Mail-Integration -- beides liess sich
    dort sogar unabhängig voneinander umschalten. Jetzt: oben nur Projekte,
    unten nur Mails."""
    from fastapi.testclient import TestClient

    from web.main import app

    pid = queries.insert_project(tmp_db, "P", "/scan/p")
    tmp_db.execute("UPDATE projects SET active=1 WHERE id=?", (pid,))
    tmp_db.execute(
        "INSERT INTO mail_scan_config (mailbox_name, active, project_id) VALUES (?, 1, ?)",
        ("Postfach/Projekt-P", pid),
    )
    tmp_db.commit()

    projects_html = TestClient(app).get("/dashboard/projects/list").text
    assert "Postfach/Projekt-P" not in projects_html

    mail_html = TestClient(app).get("/dashboard/mail").text
    assert "Postfach/Projekt-P" in mail_html
