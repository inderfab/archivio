import re
import time
from pathlib import Path

import pytest

from scanner.ablage import kontext, vorgang
from scanner.walker import scan_project
from tests.test_ablage_transport import (  # noqa: F401  (Fixtures und Helfer wiederverwenden)
    _Sitzung, _ausfuehren, _bestaetigen, _datei, _drop, at, welt,
)


def _struktur_scannen(w):
    """Projektordner mit ein paar Dateien scannen, damit Ordner (und damit Optionen) existieren."""
    for rel, namen in {"c_Protokolle": ["Protokoll BHS 1.txt", "Protokoll BHS 2.txt"],
                       "51_Ausfuehrung/51a_Planstaende": ["Plan a.txt"]}.items():
        d = w["projekt"] / rel
        d.mkdir(parents=True, exist_ok=True)
        for n in namen:
            (d / n).write_text(f"inhalt {n}", encoding="utf-8")
    scan_project(w["pid"], w["projekt"])
    kontext.invalidieren()


def _seite(w, *tokens):
    r = w["client"].get("/dashboard/ablage?t=" + ",".join(tokens))
    assert r.status_code == 200
    return r.text


# ── Seite ──────────────────────────────────────────────────────────────────────

def test_seite_ohne_token_und_abgelaufen(welt):
    assert "aufs Archivio-Symbol" in _seite(welt)
    assert "abgelaufen" in _seite(welt, "gibtesnichtabcdefgh")


def test_seite_zeigt_projekt_dateiname_und_ziel(welt):
    _struktur_scannen(welt)
    p = _datei(welt, "211_Protokoll BHS 9.txt")
    token = _drop(welt, p)
    html = _seite(welt, token)
    assert "211_Protokoll BHS 9.txt" in html and "211 Emmenhof" in html
    assert f'data-token="{token}"' in html
    assert "ad-opt" in html                                  # Optionen sind schon berechnet
    assert 'id="ablegen-knopf"' in html and "disabled" in html     # bis ein Ziel feststeht
    assert "htmx" in html or "hx-" in html or "browse-cols" in html
    assert "Anderen Ordner wählen" in html


def test_seite_projekt_unklar_zeigt_projektwahl(welt, tmp_path):
    zweites = tmp_path / "Projekte" / "212 Seepark"
    zweites.mkdir()
    welt["conn"].execute("INSERT INTO projects (name, path) VALUES (?, ?)", ("212 Seepark", str(zweites)))
    welt["conn"].commit()
    token = _drop(welt, _datei(welt, "Dokument.txt", "nichts Besonderes"))
    html = _seite(welt, token)
    assert "Zu welchem Projekt" in html and "ad-projekt" in html
    assert "211 Emmenhof" in html and "212 Seepark" in html
    assert "hx-get" in html and "projekt=" in html


def test_projekt_von_hand_waehlen_tauscht_die_datei_aus(welt, tmp_path):
    zweites = tmp_path / "Projekte" / "212 Seepark"
    (zweites / "d_Fotografie").mkdir(parents=True)
    pid2 = welt["conn"].execute("INSERT INTO projects (name, path) VALUES (?, ?)", ("212 Seepark", str(zweites))).lastrowid
    welt["conn"].commit()
    token = _drop(welt, _datei(welt, "Dokument.txt", "nichts Besonderes"))
    r = welt["client"].get(f"/dashboard/ablage/datei?token={token}&idx=0&projekt={pid2}")
    assert r.status_code == 200
    assert "Projekt <strong>212 Seepark</strong>" in r.text and 'id="datei-0"' in r.text
    gespeichert = vorgang.holen(welt["conn"], token)["vorschlag"]
    assert gespeichert["projekte"][0]["id"] == pid2 and gespeichert["projekte"][0]["p"] == 1.0
    assert welt["client"].get("/dashboard/ablage/datei?token=gibtesnichtabcdef&idx=0").status_code == 404


def test_duplikat_wird_angezeigt(welt):
    _struktur_scannen(welt)
    vorhanden = welt["projekt"] / "c_Protokolle" / "Protokoll BHS 1.txt"
    p = welt["desktop"] / "Protokoll BHS 1.txt"
    p.write_text(vorhanden.read_text(encoding="utf-8"), encoding="utf-8")        # identischer Inhalt
    token = _drop(welt, p)
    html = _seite(welt, token)
    assert "liegt bereits im Archiv" in html and "Protokoll BHS 1.txt" in html
    assert "Nicht ablegen" in html and "Trotzdem ablegen" in html


def test_spaltenbrowser_bis_zum_vorschlag(welt):
    _struktur_scannen(welt)
    ziel = welt["projekt"] / "51_Ausfuehrung" / "51a_Planstaende"
    r = welt["client"].get("/dashboard/ablage-spalten", params={"start": str(welt["projekt"]), "dest": str(ziel), "idx": 3})
    assert r.status_code == 200 and "51a_Planstaende" in r.text and "c_Protokolle" in r.text
    assert "browse-cols-3" in r.text                          # Spalten gehören zur Datei 3 (nicht zu „Projekt 0")
    r = welt["client"].get("/dashboard/ablage-spalten", params={"start": str(welt["projekt"]), "idx": 0})
    assert r.status_code == 200 and "c_Protokolle" in r.text
    # „Ordner unklar": sicher_bis ist die Projektwurzel selbst → trotzdem die erste Ebene zeigen, nicht eine leere Fläche
    r = welt["client"].get("/dashboard/ablage-spalten",
                           params={"start": str(welt["projekt"]), "dest": str(welt["projekt"]), "idx": 0})
    assert r.status_code == 200 and "c_Protokolle" in r.text and "51_Ausfuehrung" in r.text


def test_alte_upload_adresse(welt):
    p = _datei(welt)
    r = welt["client"].get("/dashboard/upload", params={"src": str(p)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/dashboard/ablage?t=")
    assert welt["client"].get("/dashboard/upload", params={"src": "/gibt/es/nicht"}).status_code == 410
    assert welt["client"].post("/dashboard/upload", data={"src": str(p)}).status_code in (404, 405)


# ── Rückfrage bei gleichnamiger Datei ──────────────────────────────────────────

def test_pruefen_gleichnamig(welt):
    ziel = welt["projekt"] / "c_Protokolle"
    (ziel / "Protokoll BHS 5.txt").write_text("alt", encoding="utf-8")        # nicht indexiert, nur auf der Platte
    token = _drop(welt, _datei(welt))
    r = welt["client"].get(f"/api/ablage/{token}/pruefen", params={"dest": str(ziel), "dateiname": "Protokoll BHS 5.txt"})
    j = r.json()
    assert j["gleichnamig"]["exakt"] is True and j["gleichnamig"]["pfad"].endswith("Protokoll BHS 5.txt")
    r = welt["client"].get(f"/api/ablage/{token}/pruefen", params={"dest": str(ziel), "dateiname": "Anderer Name.txt"})
    assert r.json()["gleichnamig"] is None
    r = welt["client"].get(f"/api/ablage/{token}/pruefen", params={"dest": str(welt["fremd"]), "dateiname": "x.txt"})
    assert r.json()["ausserhalb"] is True
    assert welt["client"].get("/api/ablage/gibtesnichtabcdef/pruefen", params={"dest": "/x", "dateiname": "y"}).status_code == 404


def test_archivieren_auch_wenn_nur_auf_der_platte(welt):
    """Gleichnamige Datei, die noch nicht gescannt wurde: Archivieren muss trotzdem klappen."""
    ziel = welt["projekt"] / "c_Protokolle"
    alt = ziel / "Protokoll BHS 5.txt"
    alt.write_text("alter Stand", encoding="utf-8")
    token = _drop(welt, _datei(welt, inhalt="neuer Stand"))
    assert _bestaetigen(welt, token, ziel, vorgaenger_modus="archivieren").status_code == 200
    code, res = _ausfuehren(welt, token)
    assert code == 200
    assert (ziel / "z_Archiv" / "Protokoll BHS 5.txt").read_text(encoding="utf-8") == "alter Stand"
    assert alt.read_text(encoding="utf-8") == "neuer Stand"


# ── Mehrfach-Drop (§6.6) ───────────────────────────────────────────────────────

def test_mehrfachdrop_projekt_wird_uebernommen(welt, tmp_path):
    zweites = tmp_path / "Projekte" / "212 Seepark"
    zweites.mkdir()
    welt["conn"].execute("INSERT INTO projects (name, path) VALUES (?, ?)", ("212 Seepark", str(zweites)))
    welt["conn"].commit()
    sicher = _drop(welt, _datei(welt, "211_Protokoll BHS 1.txt"))
    unklar = _drop(welt, _datei(welt, "Dokument ohne Hinweis.txt", "xyz"))
    assert vorgang.holen(welt["conn"], unklar)["vorschlag"]["fall"] == "projekt_unklar"
    html = _seite(welt, sicher, unklar)
    assert html.count('class="ad"') == 2 or html.count('<section class="ad"') == 2
    v = vorgang.holen(welt["conn"], unklar)["vorschlag"]
    assert v["projekte"][0]["name"] == "211 Emmenhof" and v["angeglichen"] is True
    assert "von einer anderen Datei" in v["projekte"][0]["gruende"][0]
    assert "Alle wie erste" in html


def test_mehrfachdrop_gleicher_stamm_gleicher_ordner(welt):
    _struktur_scannen(welt)
    t1 = _drop(welt, _datei(welt, "211_Protokoll BHS 11.txt", "a"))
    t2 = _drop(welt, _datei(welt, "211_Protokoll BHS 12.txt", "b"))
    v1, v2 = vorgang.holen(welt["conn"], t1)["vorschlag"], vorgang.holen(welt["conn"], t2)["vorschlag"]
    _seite(welt, t1, t2)
    n2 = vorgang.holen(welt["conn"], t2)["vorschlag"]
    assert [o["pfad"] for o in n2["optionen"]] == [o["pfad"] for o in vorgang.holen(welt["conn"], t1)["vorschlag"]["optionen"]]


def test_gruppe_ohne_wirkung_bei_einer_datei(welt):
    t = _drop(welt, _datei(welt))
    vorher = vorgang.holen(welt["conn"], t)["vorschlag"]
    _seite(welt, t)
    assert vorgang.holen(welt["conn"], t)["vorschlag"] == vorher


# ── Rückgängig (Helper) ────────────────────────────────────────────────────────

def _abgelegt(w, **extra):
    p = _datei(w, inhalt=extra.pop("inhalt", "neu"))
    token = _drop(w, p)
    _bestaetigen(w, token, extra.pop("ziel", w["projekt"] / "c_Protokolle"), **extra)
    code, res = _ausfuehren(w, token)
    assert code == 200
    return p, token, res


def _zurueck(w, token):
    return at.rueckgaengig(token, w["reg"], _Sitzung.BASIS, session=w["sitzung"])


def test_rueckgaengig_bringt_die_datei_zurueck_und_bucht_um(welt):
    p, token, res = _abgelegt(welt)
    endgueltig = Path(res["final_path"])
    assert endgueltig.is_file() and not p.exists()
    assert welt["conn"].execute("SELECT COUNT(*) FROM ablage_log").fetchone()[0] == 1
    code, antwort = _zurueck(welt, token)
    assert code == 200 and p.is_file() and not endgueltig.exists()
    assert vorgang.holen(welt["conn"], token)["status"] == "abgebrochen"
    assert welt["conn"].execute("SELECT COUNT(*) FROM ablage_log").fetchone()[0] == 0
    assert welt["conn"].execute("SELECT 1 FROM document_paths WHERE path = ?", (str(endgueltig),)).fetchone() is None
    assert _zurueck(welt, token)[0] == 403                     # ein zweites Mal gibt es nicht


def test_rueckgaengig_nach_5_minuten_nicht_mehr(welt):
    p, token, res = _abgelegt(welt)
    welt["reg"]._d[token]["ts_fertig"] -= at.RUECKGAENGIG_S + 5
    code, antwort = _zurueck(welt, token)
    assert code == 409 and "5 Minuten" in antwort["error"] and not p.exists()


def test_rueckgaengig_nicht_bei_kopie_und_nicht_bei_ueberschreiben(welt):
    p, token, res = _abgelegt(welt, kopie=True)
    assert _zurueck(welt, token)[0] == 409
    assert p.exists() and Path(res["final_path"]).exists()


def test_rueckgaengig_ueberschreiben_nicht_moeglich(welt):
    ziel = welt["projekt"] / "c_Protokolle"
    (ziel / "Protokoll BHS 5.txt").write_text("alt", encoding="utf-8")
    p, token, res = _abgelegt(welt, vorgaenger_modus="ueberschreiben")
    assert res["ersetzt"]
    code, antwort = _zurueck(welt, token)
    assert code == 409 and "Überschrieben" in antwort["error"]


def test_rueckgaengig_stellt_archivierten_vorgaenger_wieder_her(welt):
    ziel = welt["projekt"] / "c_Protokolle"
    alt = ziel / "Protokoll BHS 5.txt"
    alt.write_text("alter Stand", encoding="utf-8")
    p, token, res = _abgelegt(welt, vorgaenger_modus="archivieren", inhalt="neuer Stand")
    assert (ziel / "z_Archiv" / alt.name).exists()
    assert _zurueck(welt, token)[0] == 200
    assert alt.read_text(encoding="utf-8") == "alter Stand" and p.read_text(encoding="utf-8") == "neuer Stand"
    assert not (ziel / "z_Archiv" / alt.name).exists()


def test_rueckgaengig_nicht_wenn_ursprungsort_belegt(welt):
    p, token, res = _abgelegt(welt)
    p.write_text("etwas anderes", encoding="utf-8")
    code, _ = _zurueck(welt, token)
    assert code == 409 and p.read_text(encoding="utf-8") == "etwas anderes" and Path(res["final_path"]).exists()


def test_rueckgaengig_fremder_token(welt):
    assert _zurueck(welt, "erfunden-token-1234")[0] == 403
    token = _drop(welt, _datei(welt))                                   # nie abgelegt
    assert _zurueck(welt, token)[0] == 403


def test_bridge_rueckgaengig_nur_mit_token(welt):
    import logging
    import threading
    from http.server import HTTPServer
    import requests
    from tests.test_ablage_transport import bridge
    handler = bridge.make_local_http_handler("T", logging.getLogger("t"), config_provider=lambda: _Sitzung.BASIS,
                                             ablage_registry=welt["reg"])
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = requests.post(f"http://127.0.0.1:{srv.server_address[1]}/ablage/rueckgaengig",
                          json={"token": "erraten", "src": "/etc/passwd"}, timeout=10)
        assert r.status_code == 403
    finally:
        srv.shutdown()


# ── Rückgängig (Server-Mac) ────────────────────────────────────────────────────

def test_rueckgaengig_lokal(welt):
    p = _datei(welt)
    token = welt["client"].post("/api/ablage/analyse-lokal", json={"src": str(p)}).json()["token"]
    ziel = welt["projekt"] / "c_Protokolle"
    _bestaetigen(welt, token, ziel)
    assert welt["client"].post(f"/api/ablage/{token}/ausfuehren-lokal").status_code == 200
    assert not p.exists()
    r = welt["client"].post(f"/api/ablage/{token}/rueckgaengig-lokal")
    assert r.status_code == 200 and p.exists() and not (ziel / p.name).exists()
    assert vorgang.holen(welt["conn"], token)["status"] == "abgebrochen"
    assert welt["client"].post(f"/api/ablage/{token}/rueckgaengig-lokal").status_code == 409


def test_rueckgaengig_lokal_kopie_und_frist(welt):
    p = _datei(welt)
    token = welt["client"].post("/api/ablage/analyse-lokal", json={"src": str(p)}).json()["token"]
    _bestaetigen(welt, token, welt["projekt"] / "c_Protokolle", kopie=True)
    welt["client"].post(f"/api/ablage/{token}/ausfuehren-lokal")
    assert welt["client"].post(f"/api/ablage/{token}/rueckgaengig-lokal").status_code == 409
    p2 = _datei(welt, "Zweite.txt")
    t2 = welt["client"].post("/api/ablage/analyse-lokal", json={"src": str(p2)}).json()["token"]
    _bestaetigen(welt, t2, welt["projekt"] / "c_Protokolle")
    welt["client"].post(f"/api/ablage/{t2}/ausfuehren-lokal")
    import json as _j
    v = vorgang.holen(welt["conn"], t2)
    e = v["entscheid"]
    e["abgelegt_ts"] = "2020-01-01T00:00:00Z"
    welt["conn"].execute("UPDATE ablage_vorgang SET entscheid = ? WHERE token = ?", (_j.dumps(e), t2))
    welt["conn"].commit()
    r = welt["client"].post(f"/api/ablage/{t2}/rueckgaengig-lokal")
    assert r.status_code == 409 and "5 Minuten" in r.json()["error"]


# ── Aufbau der Seite ───────────────────────────────────────────────────────────

def test_skript_der_seite_ist_syntaktisch_gueltig(welt, tmp_path):
    import shutil
    import subprocess
    node = shutil.which("node")
    jsc = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"
    if not node and not Path(jsc).exists():
        pytest.skip("weder node noch jsc vorhanden")
    html = _seite(welt, _drop(welt, _datei(welt)))
    skripte = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    assert skripte
    f = tmp_path / "seite.js"
    f.write_text(max(skripte, key=len), encoding="utf-8")
    if node:
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    else:       # jsc: Funktion bauen, ohne sie auszuführen (nur Syntax)
        pruefer = tmp_path / "pruefen.js"
        pruefer.write_text("var src = readFile(%r); new Function(src); print('ok');" % str(f), encoding="utf-8")
        r = subprocess.run([jsc, str(pruefer)], capture_output=True, text=True)
        assert "ok" in r.stdout, r.stdout + r.stderr
    assert r.returncode == 0, r.stderr
