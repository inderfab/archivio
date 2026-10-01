import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "shared"))
import ablage_transport as at  # noqa: E402
import menubar_bridge as bridge  # noqa: E402

from db import connection  # noqa: E402
from scanner.ablage import vorgang  # noqa: E402


class _Antwort:
    def __init__(self, r):
        self._r = r
        self.status_code = r.status_code

    def json(self):
        return self._r.json()


class _Sitzung:
    """Leitet requests-Aufrufe des Helpers an die FastAPI-App (TestClient) um — echter Weg, ohne Netzwerk."""
    BASIS = "http://server.test"

    def __init__(self, client):
        self.c = client

    def _pfad(self, url):
        assert url.startswith(self.BASIS), url
        return url[len(self.BASIS):]

    def get(self, url, **kw):
        return _Antwort(self.c.get(self._pfad(url)))

    def post(self, url, data=None, files=None, json=None, **kw):
        return _Antwort(self.c.post(self._pfad(url), data=data, files=files, json=json))


@pytest.fixture
def welt(tmp_db, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from web.main import app
    monkeypatch.delenv("ARCHIVIO_UPLOAD_DRY_RUN", raising=False)
    projekt = tmp_path / "Projekte" / "211 Emmenhof"
    (projekt / "51_Ausfuehrung" / "51a_Planstaende").mkdir(parents=True)
    (projekt / "c_Protokolle").mkdir(parents=True)
    fremd = tmp_path / "Privat"
    fremd.mkdir()
    pid = tmp_db.execute("INSERT INTO projects (name, path) VALUES (?, ?)", ("211 Emmenhof", str(projekt))).lastrowid
    tmp_db.commit()
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    client = TestClient(app)
    return {"conn": tmp_db, "client": client, "sitzung": _Sitzung(client), "projekt": projekt, "pid": pid,
            "desktop": desktop, "fremd": fremd, "reg": at.TokenRegistry()}


def _datei(w, name="Protokoll BHS 5.txt", inhalt="Protokoll der Bauherrensitzung"):
    p = w["desktop"] / name
    p.write_text(inhalt, encoding="utf-8")
    return p


def _drop(w, p):
    res = at.dateien_analysieren([str(p)], _Sitzung.BASIS, w["reg"], session=w["sitzung"], host="mac-1")
    assert res and res[0].get("token"), res
    return res[0]["token"]


def _bestaetigen(w, token, dest, **extra):
    return w["client"].post(f"/api/ablage/{token}/bestaetigen", json={"dest": str(dest), **extra})


def _ausfuehren(w, token):
    return at.ausfuehren(token, w["reg"], _Sitzung.BASIS, session=w["sitzung"])


# ── Analyse ────────────────────────────────────────────────────────────────────

def test_drop_analyse_registriert_token_und_speichert_vorgang(welt):
    p = _datei(welt)
    token = _drop(welt, p)
    assert welt["reg"].get(token)["src"] == str(p)
    v = vorgang.holen(welt["conn"], token)
    assert v["status"] == "offen" and v["dateiname"] == p.name and v["host"] == "mac-1"
    assert v["hash"] == at.sha256_datei(str(p))
    assert v["staging_pfad"] and Path(v["staging_pfad"]).is_file()           # Textformat wurde hochgeladen
    assert "ext:.txt" in v["merkmale"] and v["vorschlag"]["fall"]
    r = welt["client"].get(f"/api/ablage/{token}")
    assert r.status_code == 200 and r.json()["status"] == "offen"


def test_cad_dateien_werden_nicht_hochgeladen_nur_metadaten(welt):
    p = welt["desktop"] / "Plan.dwg"
    p.write_bytes(b"\x00\x01" * 100)
    token = _drop(welt, p)
    v = vorgang.holen(welt["conn"], token)
    assert v["staging_pfad"] is None and v["groesse"] == 200 and v["hash"]


def test_ordner_werden_nicht_abgelegt(welt):
    res = at.dateien_analysieren([str(welt["desktop"])], _Sitzung.BASIS, welt["reg"], session=welt["sitzung"])
    assert res[0].get("fehler") and not len(welt["reg"])


def test_extraktion_hartes_limit(tmp_path, monkeypatch):
    from scanner import extractors

    def langsam(path):
        time.sleep(5)
        return [{"content": "x"}]
    monkeypatch.setattr(extractors, "extract_chunks", langsam)
    p = tmp_path / "a.pdf"
    p.write_bytes(b"x")
    t0 = time.time()
    assert vorgang.text_extrahieren(p, timeout=0.3) is None
    assert time.time() - t0 < 2


def test_extraktion_auf_3000_zeichen_begrenzt(tmp_path, monkeypatch):
    from scanner import extractors
    monkeypatch.setattr(extractors, "extract_chunks", lambda p: [{"content": "wort " * 2000}])
    assert len(vorgang.text_extrahieren(tmp_path / "x.pdf")) == 3000


# ── Bestätigen (Server prüft das Ziel) ─────────────────────────────────────────

def test_ziel_ausserhalb_eines_projekts_abgelehnt(welt):
    token = _drop(welt, _datei(welt))
    r = _bestaetigen(welt, token, welt["fremd"])
    assert r.status_code == 400 and "Projekt" in r.json()["error"]
    assert vorgang.holen(welt["conn"], token)["status"] == "offen"


def test_ziel_mit_punkt_punkt_und_ungueltiger_name_abgelehnt(welt):
    token = _drop(welt, _datei(welt))
    assert _bestaetigen(welt, token, str(welt["projekt"]) + "/../Privat").status_code == 400
    assert _bestaetigen(welt, token, welt["projekt"] / "c_Protokolle", dateiname="../x.txt").status_code == 400
    assert _bestaetigen(welt, token, welt["projekt"] / "c_Protokolle", dateiname="a/b.txt").status_code == 400
    assert _bestaetigen(welt, token, "relativ/pfad").status_code == 400


def test_projekt_mit_aehnlichem_namen_ist_kein_treffer(welt, tmp_path):
    aehnlich = tmp_path / "Projekte" / "211 Emmenhof2"       # liegt NICHT unter 211 Emmenhof
    aehnlich.mkdir()
    token = _drop(welt, _datei(welt))
    assert _bestaetigen(welt, token, aehnlich).status_code == 400


def test_musterordner_ist_nie_ein_ziel(welt, tmp_path, monkeypatch):
    from config import settings
    muster = tmp_path / "Projekte" / "000 Musterordner Objekt"
    muster.mkdir()
    welt["conn"].execute("INSERT INTO projects (name, path) VALUES (?, ?)", ("000 Musterordner Objekt", str(muster)))
    welt["conn"].commit()
    monkeypatch.setattr(settings, "_settings", {**settings._load(), "ablage": {"musterordner": str(muster)}})
    token = _drop(welt, _datei(welt))
    assert _bestaetigen(welt, token, muster).status_code == 400


def test_unbekannter_vorgang(welt):
    assert _bestaetigen(welt, "gibtesnicht1234", welt["projekt"]).status_code == 400
    assert welt["client"].get("/api/ablage/gibtesnicht1234").status_code == 404
    assert welt["client"].get("/api/ablage/a/entscheid").status_code == 404


# ── Ausführen (Helper) ─────────────────────────────────────────────────────────

def test_ganzer_ablauf_verschiebt_protokolliert_und_indexiert(welt):
    p = _datei(welt)
    token = _drop(welt, p)
    ziel = welt["projekt"] / "c_Protokolle"
    assert _bestaetigen(welt, token, ziel).status_code == 200
    code, res = _ausfuehren(welt, token)
    assert code == 200 and res["ok"] and not res["trocken"]
    endgueltig = ziel / "Protokoll BHS 5.txt"
    assert endgueltig.is_file() and not p.exists()                    # verschoben
    assert res["final_path"] == str(endgueltig)
    v = vorgang.holen(welt["conn"], token)
    assert v["status"] == "abgelegt"
    assert not Path(v["staging_pfad"]).exists()                        # Staging aufgeräumt
    log_zeile = welt["conn"].execute("SELECT * FROM ablage_log").fetchone()
    assert log_zeile["gewaehlt_pfad"] == str(ziel) and log_zeile["host"] == "mac-1" and log_zeile["endung"] == ".txt"
    assert log_zeile["dauer_ms"] is not None
    doc = welt["conn"].execute("SELECT d.filename FROM documents d JOIN document_paths dp ON dp.document_id = d.id "
                               "WHERE dp.path = ?", (str(endgueltig),)).fetchone()
    assert doc is not None                                             # sofort durchsuchbar
    assert welt["reg"].get(token)["fertig"] is True                    # Token verbraucht (bleibt nur für „Rückgängig")
    assert _ausfuehren(welt, token)[0] == 403                          # und lässt sich nicht nochmals ausführen


def test_kopie_behaelt_das_original(welt):
    p = _datei(welt)
    token = _drop(welt, p)
    _bestaetigen(welt, token, welt["projekt"] / "c_Protokolle", kopie=True)
    code, _ = _ausfuehren(welt, token)
    assert code == 200 and p.exists() and (welt["projekt"] / "c_Protokolle" / p.name).exists()


def test_nicht_bestaetigt_wird_nicht_ausgefuehrt(welt):
    p = _datei(welt)
    token = _drop(welt, p)
    code, res = _ausfuehren(welt, token)
    assert code == 409 and p.exists()


def test_neuer_ordner_eine_ebene_wird_angelegt(welt):
    p = _datei(welt)
    token = _drop(welt, p)
    neu = welt["projekt"] / "c_Protokolle" / "1_Bauherrensitzungen_BHS"
    assert _bestaetigen(welt, token, neu).status_code == 200
    code, res = _ausfuehren(welt, token)
    assert code == 200 and (neu / p.name).is_file()


def test_neuer_ordner_zwei_ebenen_tief_abgelehnt(welt):
    token = _drop(welt, _datei(welt))
    assert _bestaetigen(welt, token, welt["projekt"] / "x" / "y").status_code == 400


# ── Namenskollision, Vorgänger ─────────────────────────────────────────────────

def test_namenskollision_ergibt_zweiten_namen(welt):
    ziel = welt["projekt"] / "c_Protokolle"
    (ziel / "Protokoll BHS 5.txt").write_text("alt", encoding="utf-8")
    token = _drop(welt, _datei(welt))
    _bestaetigen(welt, token, ziel)                                    # Modus „behalten" ist der Standard
    code, res = _ausfuehren(welt, token)
    assert Path(res["final_path"]).name == "Protokoll BHS 5 (2).txt"
    assert (ziel / "Protokoll BHS 5.txt").read_text(encoding="utf-8") == "alt"      # nichts überschrieben


def test_vorgaenger_archivieren(welt):
    from scanner.ablage import kontext
    ziel = welt["projekt"] / "c_Protokolle"
    alt = ziel / "Protokoll BHS 5.txt"
    alt.write_text("alter Stand", encoding="utf-8")
    (ziel / "z_Archiv").mkdir()
    # Der Vorgänger muss dem Server bekannt sein (indexiert + Ordner erfasst)
    from scanner.walker import scan_project
    scan_project(welt["pid"], welt["projekt"])
    kontext.invalidieren()
    token = _drop(welt, _datei(welt, inhalt="neuer Stand"))
    assert _bestaetigen(welt, token, ziel, vorgaenger_modus="archivieren").status_code == 200
    e = vorgang.holen(welt["conn"], token)["entscheid"]
    assert e["vorgaenger_modus"] == "archivieren" and e["vorgaenger"] == str(alt)
    code, res = _ausfuehren(welt, token)
    assert code == 200
    assert (ziel / "z_Archiv" / "Protokoll BHS 5.txt").read_text(encoding="utf-8") == "alter Stand"
    assert (ziel / "Protokoll BHS 5.txt").read_text(encoding="utf-8") == "neuer Stand"
    assert res["archiviert"] == str(ziel / "z_Archiv" / "Protokoll BHS 5.txt")


def test_vorgaenger_ueberschreiben(welt):
    from scanner.walker import scan_project
    from scanner.ablage import kontext
    ziel = welt["projekt"] / "c_Protokolle"
    (ziel / "Protokoll BHS 5.txt").write_text("alter Stand", encoding="utf-8")
    scan_project(welt["pid"], welt["projekt"])
    kontext.invalidieren()
    p = _datei(welt, inhalt="neuer Stand")
    token = _drop(welt, p)
    _bestaetigen(welt, token, ziel, vorgaenger_modus="ueberschreiben")
    code, res = _ausfuehren(welt, token)
    assert code == 200 and res["ersetzt"] == str(ziel / "Protokoll BHS 5.txt")
    assert (ziel / "Protokoll BHS 5.txt").read_text(encoding="utf-8") == "neuer Stand"
    assert not list(ziel.glob("*.archivio-neu")) and not p.exists()


def test_archivieren_ohne_vorgaenger_faellt_auf_behalten_zurueck(welt):
    token = _drop(welt, _datei(welt))
    _bestaetigen(welt, token, welt["projekt"] / "c_Protokolle", vorgaenger_modus="archivieren")
    assert vorgang.holen(welt["conn"], token)["entscheid"]["vorgaenger_modus"] == "behalten"


# ── Sicherheit ─────────────────────────────────────────────────────────────────

def test_fremder_token_wird_nie_ausgefuehrt(welt):
    p = _datei(welt)
    fremd = _datei(welt, "Geheim.txt", "geheim")
    token = _drop(welt, p)
    _bestaetigen(welt, token, welt["projekt"] / "c_Protokolle")
    code, res = at.ausfuehren("erfundener-token-123456", welt["reg"], _Sitzung.BASIS, session=welt["sitzung"])
    assert code == 403 and not res["ok"]
    for schlecht in (None, "", 5, ["a"], {"x": 1}):
        assert at.ausfuehren(schlecht, welt["reg"], _Sitzung.BASIS, session=welt["sitzung"])[0] == 403
    assert p.exists() and fremd.exists()                                  # nichts bewegt


def test_token_vom_server_aber_nicht_in_der_helper_registry(welt):
    """Ein Vorgang, den ein ANDERER Rechner angelegt hat, darf dieser Helper nie ausführen."""
    token = _drop(welt, _datei(welt))
    _bestaetigen(welt, token, welt["projekt"] / "c_Protokolle")
    anderer_helper = at.TokenRegistry()
    code, _ = at.ausfuehren(token, anderer_helper, _Sitzung.BASIS, session=welt["sitzung"])
    assert code == 403


def test_helper_prueft_ziel_nochmals_auch_wenn_der_server_luegt(welt):
    """Ein kompromittierter/gefälschter Server darf den Helper nicht zu Pfaden ausserhalb des Projekts bewegen."""
    p = _datei(welt)
    welt["reg"].add("tok-12345678", str(p), p.name)

    class _Boeser:
        def get(self, url, **kw):
            class R:
                status_code = 200
                def json(s):
                    return {"status": "bestaetigt", "dest": str(welt["fremd"]), "dateiname": p.name, "kopie": False,
                            "projekt_pfad": str(welt["projekt"]), "vorgaenger_modus": "behalten"}
            return R()
        post = get
    code, res = at.ausfuehren("tok-12345678", welt["reg"], _Sitzung.BASIS, session=_Boeser())
    assert code == 403 and p.exists() and not (welt["fremd"] / p.name).exists()

    class _Boeser2(_Boeser):
        def get(self, url, **kw):
            r = super().get(url)
            d = r.json()
            d.update(dest=str(welt["projekt"] / "c_Protokolle"), vorgaenger=str(welt["fremd"] / "x.txt"),
                     vorgaenger_modus="archivieren")
            r.json = lambda: d
            return r
    assert at.ausfuehren("tok-12345678", welt["reg"], _Sitzung.BASIS, session=_Boeser2())[0] == 403
    assert p.exists()


def test_ist_unterhalb():
    assert at.ist_unterhalb("/a/b/c", "/a/b") and at.ist_unterhalb("/a/b", "/a/b")
    assert not at.ist_unterhalb("/a/b2", "/a/b") and not at.ist_unterhalb("/a/b/../x", "/a/b")
    assert not at.ist_unterhalb("rel", "/a") and not at.ist_unterhalb("", "/a") and not at.ist_unterhalb("/a", "")
    import unicodedata
    assert at.ist_unterhalb(unicodedata.normalize("NFD", "/a/Planstände/x"), "/a/Planstände")


def test_dateiname_ok():
    for gut in ("a.pdf", "211_Plan (2).pdf", "Plan Ä.pdf"):
        assert at.dateiname_ok(gut)
    for schlecht in ("", " a", "a/b", "..", ".", ".versteckt", "a\x00b"):
        assert not at.dateiname_ok(schlecht)


# ── Trockenlauf ────────────────────────────────────────────────────────────────

def test_trockenlauf_verschiebt_nichts(welt, monkeypatch):
    p = _datei(welt)
    token = _drop(welt, p)
    ziel = welt["projekt"] / "c_Protokolle"
    _bestaetigen(welt, token, ziel)
    monkeypatch.setenv("ARCHIVIO_UPLOAD_DRY_RUN", "1")
    code, res = _ausfuehren(welt, token)
    assert code == 200 and res["trocken"] is True
    assert p.exists() and not (ziel / p.name).exists()
    assert welt["reg"].get(token) is not None                           # Token bleibt, nochmals möglich
    assert vorgang.holen(welt["conn"], token)["status"] == "bestaetigt"
    assert welt["conn"].execute("SELECT COUNT(*) FROM ablage_log").fetchone()[0] == 0
    monkeypatch.delenv("ARCHIVIO_UPLOAD_DRY_RUN")
    assert _ausfuehren(welt, token)[0] == 200 and (ziel / p.name).exists()


# ── Aufräumen ──────────────────────────────────────────────────────────────────

def test_aufraeumen_nach_24_stunden(welt):
    token = _drop(welt, _datei(welt))
    v = vorgang.holen(welt["conn"], token)
    staging = Path(v["staging_pfad"]).parent
    assert staging.is_dir()
    assert vorgang.aufraeumen(welt["conn"]) == 0                         # frisch: bleibt
    alt = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
    welt["conn"].execute("UPDATE ablage_vorgang SET erstellt = ?", (alt,))
    welt["conn"].execute("INSERT INTO ablage_log (ts, dateiname) VALUES ('2026-01-01T00:00:00Z', 'bleibt.pdf')")
    welt["conn"].commit()
    assert vorgang.aufraeumen(welt["conn"]) == 1
    assert not staging.exists()
    assert vorgang.holen(welt["conn"], token) is None
    assert welt["conn"].execute("SELECT COUNT(*) FROM ablage_log").fetchone()[0] == 1       # Protokoll bleibt


def test_abbrechen_loescht_staging_nicht_aber_die_quelle(welt):
    p = _datei(welt)
    token = _drop(welt, p)
    staging = Path(vorgang.holen(welt["conn"], token)["staging_pfad"])
    assert welt["client"].post(f"/api/ablage/{token}/abbrechen").status_code == 200
    assert not staging.exists() and p.exists()                           # die Datei des Nutzers bleibt unberührt


# ── Drop am Server-Mac ─────────────────────────────────────────────────────────

def test_lokal_nur_von_localhost(welt, monkeypatch):
    from web import ablage_api
    p = _datei(welt)
    assert welt["client"].post("/api/ablage/analyse-lokal", json={"src": str(p)}).status_code == 200   # „lokal"
    # Ein Rechner im LAN (hier: der Test-Client ist nicht mehr in der Liste lokaler Adressen) darf das nie
    monkeypatch.setattr(ablage_api, "_LOKAL", ("127.0.0.1", "::1", "localhost"))
    assert welt["client"].post("/api/ablage/analyse-lokal", json={"src": str(p)}).status_code == 403
    assert welt["client"].post("/api/ablage/irgendwas123/ausfuehren-lokal").status_code == 403


def test_server_mac_ganzer_ablauf_ohne_helper(welt):
    p = _datei(welt)
    r = welt["client"].post("/api/ablage/analyse-lokal", json={"src": str(p)})
    assert r.status_code == 200
    token = r.json()["token"]
    assert welt["client"].get(f"/api/ablage/{token}").json()["lokal"] is True
    ziel = welt["projekt"] / "c_Protokolle"
    assert _bestaetigen(welt, token, ziel).status_code == 200
    r = welt["client"].post(f"/api/ablage/{token}/ausfuehren-lokal")
    assert r.status_code == 200 and r.json()["ok"]
    assert (ziel / p.name).is_file() and not p.exists()
    assert vorgang.holen(welt["conn"], token)["status"] == "abgelegt"
    assert p.parent.exists()                                              # Quellordner bleibt


def test_server_mac_nicht_bestaetigt_und_unbekannt(welt):
    p = _datei(welt)
    token = welt["client"].post("/api/ablage/analyse-lokal", json={"src": str(p)}).json()["token"]
    assert welt["client"].post(f"/api/ablage/{token}/ausfuehren-lokal").status_code == 409
    assert welt["client"].post("/api/ablage/unbekannt1234/ausfuehren-lokal").status_code == 404
    assert welt["client"].post("/api/ablage/analyse-lokal", json={"src": "/gibt/es/nicht.pdf"}).status_code == 404


def test_server_mac_trockenlauf(welt, monkeypatch):
    p = _datei(welt)
    token = welt["client"].post("/api/ablage/analyse-lokal", json={"src": str(p)}).json()["token"]
    ziel = welt["projekt"] / "c_Protokolle"
    _bestaetigen(welt, token, ziel)
    monkeypatch.setenv("ARCHIVIO_UPLOAD_DRY_RUN", "1")
    r = welt["client"].post(f"/api/ablage/{token}/ausfuehren-lokal")
    assert r.status_code == 200 and r.json()["trocken"] is True
    assert p.exists() and not (ziel / p.name).exists()


# ── Bridge: echter HTTP-Endpunkt des Helpers ───────────────────────────────────

@pytest.fixture
def bridge_server(welt, monkeypatch):
    """Die echte Bridge des Helpers, gegen die FastAPI-App als Server."""
    reg = welt["reg"]
    handler = bridge.make_local_http_handler("Test", __import__("logging").getLogger("t"),
                                             config_provider=lambda: _Sitzung.BASIS, ablage_registry=reg)
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # die Bridge ruft ablage_transport.ausfuehren(...) ohne eigene Sitzung: auf die App umleiten
    echte = at.ausfuehren
    monkeypatch.setattr(at, "ausfuehren", lambda tok, r, url, session=None, log=None, dry_run=None:
                        echte(tok, r, url, session=welt["sitzung"], log=log, dry_run=dry_run))
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _post(basis, pfad, body):
    return requests.post(basis + pfad, json=body, timeout=10)


def test_bridge_fremder_token_403_und_nichts_bewegt(welt, bridge_server):
    p = _datei(welt)
    r = _post(bridge_server, "/ablage/ausfuehren", {"token": "von-einer-webseite-erraten"})
    assert r.status_code == 403 and p.exists()
    # Freie Pfade werden gar nicht erst gelesen: nur der Token zählt
    r = _post(bridge_server, "/ablage/ausfuehren", {"src": str(p), "dest": str(welt["fremd"])})
    assert r.status_code == 403 and p.exists() and not any(welt["fremd"].iterdir())
    assert _post(bridge_server, "/ablage/ausfuehren", {}).status_code == 403
    assert requests.post(bridge_server + "/ablage/ausfuehren", data=b"kaputt", timeout=10).status_code == 403


def test_bridge_ablauf_mit_gueltigem_token(welt, bridge_server):
    p = _datei(welt)
    token = _drop(welt, p)
    ziel = welt["projekt"] / "c_Protokolle"
    _bestaetigen(welt, token, ziel)
    r = _post(bridge_server, "/ablage/ausfuehren", {"token": token})
    assert r.status_code == 200 and r.json()["ok"] and (ziel / p.name).is_file()
    # Token ist verbraucht: ein zweiter Aufruf (etwa durch eine fremde Seite) führt nichts mehr aus
    assert _post(bridge_server, "/ablage/ausfuehren", {"token": token}).status_code == 403


def test_bridge_ohne_registry_kennt_den_endpunkt_nicht(welt):
    handler = bridge.make_local_http_handler("Test", __import__("logging").getLogger("t"),
                                             config_provider=lambda: _Sitzung.BASIS)
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = requests.post(f"http://127.0.0.1:{srv.server_address[1]}/ablage/ausfuehren", json={"token": "x"}, timeout=10)
        assert r.status_code == 404
    finally:
        srv.shutdown()


def test_migration_033(tmp_db):
    ids = {r[0] for r in tmp_db.execute("SELECT id FROM _migrations")}
    assert "033_ablage_vorgang" in ids
    for t in ("ablage_vorgang", "ablage_log", "ablage_regel"):
        assert tmp_db.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (t,)).fetchone()
