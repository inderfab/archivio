"""Tests für die Onboarding-Karte im Dashboard, wenn noch kein Projektordner
konfiguriert ist (siehe web/templates/_dashboard_projects.html). Erscheint nicht nur
beim allerersten Start, sondern immer wenn aktuell kein Projekt vorhanden ist -- eine
statische In-App-Onboarding-Funktion gab es vorher nicht (siehe Plan-Recherche)."""
from db import queries


def test_onboarding_card_shown_when_no_projects_and_no_orphans(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    r = TestClient(app).get("/dashboard")
    assert r.status_code == 200
    assert "Neue Projektordner anlegen" in r.text
    assert '/dashboard/settings#ordner' in r.text


def test_onboarding_card_hidden_once_a_project_exists(tmp_db):
    from config import settings
    from fastapi.testclient import TestClient
    from web.main import app

    settings._settings.setdefault("scanner", {})["base_folders"] = [
        {"label": "Projekte", "path": "/scan"}
    ]
    queries.insert_project(tmp_db, "P", "/scan/P")
    tmp_db.commit()

    r = TestClient(app).get("/dashboard")
    assert r.status_code == 200
    assert "Neue Projektordner anlegen" not in r.text


def test_onboarding_card_hidden_when_only_orphans_exist(tmp_db):
    """Ein Projekt ausserhalb jedes konfigurierten Ordners zaehlt als 'orphan' --
    das Dashboard ist dann nicht wirklich leer, die Onboarding-Karte waere hier
    irrefuehrend (es gibt ja bereits ein Projekt, nur unter 'Nicht zugeordnet')."""
    from fastapi.testclient import TestClient
    from web.main import app

    queries.insert_project(tmp_db, "Verwaist", "/irgendwo/ausserhalb")
    tmp_db.commit()

    r = TestClient(app).get("/dashboard")
    assert r.status_code == 200
    assert "Neue Projektordner anlegen" not in r.text
