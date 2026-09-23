"""Tests für die Warteseite während der Startvorbereitung (web/main.py).

Hintergrund: die Migrationen liefen früher blockierend im lifespan, uvicorn nahm erst
danach Verbindungen an. Bei der v3.4.0-Migration sind das auf 240'000 Dokumenten rund
drei Minuten — der Nutzer sah in der Zeit nur „Verbindung fehlgeschlagen".

Sicherheitskritischer Teil: /api/status muss durchkommen und OHNE Datenbankzugriff
antworten. Daran erkennt der Watchdog der Menüleisten-App, dass der Server lebt. Täte
er es nicht, würde er den Server nach vier Fehlversuchen (60 s) mitten in der Migration
neu starten — und das immer wieder."""
import pytest
from fastapi.testclient import TestClient

from web import main as web_main


@pytest.fixture
def vorbereitung_laeuft():
    """Versetzt die App in den Zustand „Vorbereitung läuft", wie ihn der lifespan setzt."""
    vorher = dict(web_main._start_zustand)
    web_main._start_zustand.update({"laeuft": True, "seit": web_main.time.time(),
                                    "fehler": None})
    yield web_main._start_zustand
    web_main._start_zustand.clear()
    web_main._start_zustand.update(vorher)


def test_seite_zeigt_wartehinweis_statt_toter_verbindung(tmp_db, vorbereitung_laeuft):
    r = TestClient(web_main.app).get("/dashboard")
    assert r.status_code == 503
    assert "Archivio wird vorbereitet" in r.text
    assert "einige Minuten" in r.text


def test_status_antwortet_ohne_datenbank(tmp_db, vorbereitung_laeuft, monkeypatch):
    """Der Watchdog darf nicht ins Leere laufen — und die Antwort darf die gerade
    migrierende Datenbank nicht anfassen."""
    def _keine_db(*a, **k):
        raise AssertionError("Während der Vorbereitung darf die DB nicht benutzt werden")

    monkeypatch.setattr(web_main.connection, "get_connection", _keine_db)

    r = TestClient(web_main.app).get("/api/status")
    assert r.status_code == 200
    daten = r.json()
    assert daten["server"] is True
    assert daten["vorbereitung"] is True
    assert daten["seit_s"] >= 0


def test_fehler_wird_als_solcher_gezeigt(tmp_db, vorbereitung_laeuft):
    vorbereitung_laeuft["fehler"] = "database disk image is malformed"
    r = TestClient(web_main.app).get("/dashboard")
    assert r.status_code == 503
    assert "konnte nicht vorbereitet werden" in r.text
    assert "malformed" in r.text


def test_nach_der_vorbereitung_wieder_normal(tmp_db, vorbereitung_laeuft):
    vorbereitung_laeuft["laeuft"] = False
    r = TestClient(web_main.app).get("/api/status")
    assert r.status_code == 200
    assert "vorbereitung" not in r.json()
