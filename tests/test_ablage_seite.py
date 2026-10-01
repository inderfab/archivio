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


# ── Cache nicht blockierend, „Vorschläge werden vorbereitet" ───────────────────

def test_vorbereitung_wenn_der_kontext_noch_laedt(welt, monkeypatch):
    from scanner.ablage import kontext
    echt = kontext.lade_kontext

    def langsam(conn, jetzt=None):
        time.sleep(0.8)
        return echt(conn, jetzt)
    monkeypatch.setattr(kontext, "lade_kontext", langsam)
    original = kontext.holen_wartend
    monkeypatch.setattr(kontext, "holen_wartend", lambda conn, warten_s=2.0: original(conn, min(warten_s, 0.05)))
    kontext.invalidieren()
    t0 = time.time()
    token = _drop(welt, _datei(welt, "211_Protokoll BHS 1.txt"))
    assert time.time() - t0 < 0.7                                       # der Drop wartet nicht auf das Laden
    assert vorgang.holen(welt["conn"], token)["vorschlag"]["fall"] == "vorbereitung"
    html = _seite(welt, token)
    assert "Vorschläge werden vorbereitet" in html and 'hx-trigger="every 2s"' in html
    assert "Anderen Ordner wählen" in html                              # Ordnerbrowser steht trotzdem bereit
    assert 'class="ad-opt' not in html
    # sobald der Kontext da ist, rechnet die Seite den Vorschlag nach und hört auf zu pollen
    kontext._zustand["fertig"].wait(5)
    r = welt["client"].get(f"/dashboard/ablage/datei?token={token}&idx=0")
    assert r.status_code == 200 and 'hx-trigger="every 2s"' not in r.text and "211 Emmenhof" in r.text
    assert vorgang.holen(welt["conn"], token)["vorschlag"]["fall"] != "vorbereitung"


def test_kontext_cache_info_und_single_flight(welt, monkeypatch):
    from scanner.ablage import kontext
    kontext.invalidieren()
    aufrufe = []
    echt = kontext.lade_kontext
    monkeypatch.setattr(kontext, "lade_kontext", lambda c, j=None: (aufrufe.append(1), time.sleep(0.3), echt(c, j))[2])
    c = welt["conn"]
    import threading
    ergebnisse = []
    ts = [threading.Thread(target=lambda: ergebnisse.append(kontext.holen_wartend(c, 3))) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(aufrufe) == 1 and all(e is not None for e in ergebnisse)       # ein Ladevorgang für alle
    i = kontext.info()
    assert i["bereit"] and i["ordner"] >= 0 and i["ladezeit_s"] is not None and i["mb_geschaetzt"] >= 0


def test_diagnose_zeigt_den_ablage_cache(welt):
    from scanner.ablage import kontext
    kontext.holen(welt["conn"])
    r = welt["client"].get("/api/debug/diagnostics")
    assert r.status_code == 200 and "Ablage-Cache" in r.text and "Ordner" in r.text


# ── Ordnersuche (Tippen filtert) ───────────────────────────────────────────────

def _dated_projekt(w):
    p = w["projekt"]
    for rel in ("c_Protokolle/260601_BHS Start", "c_Protokolle/260615_BHS Fassade", "c_Protokolle/260701_BHS Budget",
                "c_Protokolle/260710_FPS Haustechnik", "31_Vorprojekt/31d_Fachplaner/31_Bauingenieur",
                "31_Vorprojekt/31d_Fachplaner/31_Bauingenieur/z_Archiv", "51_Ausfuehrung/51a_Planstände",
                "e_Grundlagen/Fassadenplaner", "e_Grundlagen/Upload"):
        (p / rel).mkdir(parents=True, exist_ok=True)
        (p / rel / "datei.txt").write_text(rel, encoding="utf-8")
    scan_project(w["pid"], p)
    kontext.invalidieren()


def test_suche_findet_wortteile_in_name_und_label(welt):
    from scanner.ablage import suche
    _dated_projekt(welt)
    c, pid = welt["conn"], welt["pid"]
    rels = lambda q: [t["rel"] for t in suche.suche_ordner(c, pid, q)]
    assert "31_Vorprojekt/31d_Fachplaner/31_Bauingenieur" in rels("bauing")
    assert "e_Grundlagen/Fassadenplaner" in rels("fassade") and "c_Protokolle/260615_BHS Fassade" in rels("fassade")
    beide = ["51_Ausfuehrung/51a_Planstaende", "51_Ausfuehrung/51a_Planstände"]       # ae und ä sind derselbe Ordnername
    assert sorted(rels("planstaende")) == beide == sorted(rels("PLANSTÄNDE")) == sorted(rels("planst"))
    assert rels("bhs fassade") == ["c_Protokolle/260615_BHS Fassade"]                                       # alle Wörter
    assert "31_Vorprojekt/31d_Fachplaner/31_Bauingenieur" in rels("31 bauing")
    assert suche.suche_ordner(c, pid, "") == [] and suche.suche_ordner(c, pid, "   ") == []
    assert suche.suche_ordner(c, pid, "gibtesnicht") == []


def test_suche_ohne_archiv_und_ausgeschlossene_und_nur_aus_der_db(welt, monkeypatch):
    import os
    from scanner.ablage import suche
    _dated_projekt(welt)
    c, pid = welt["conn"], welt["pid"]

    def verboten(*a, **k):
        raise AssertionError("Die Suche darf das NAS nicht anfassen")
    monkeypatch.setattr(os, "walk", verboten)
    monkeypatch.setattr(os, "scandir", verboten)
    monkeypatch.setattr(os, "listdir", verboten)
    assert not any("z_Archiv" in t["rel"] for t in suche.suche_ordner(c, pid, "archiv"))
    assert not any(t["rel"].endswith("Upload") for t in suche.suche_ordner(c, pid, "upload"))   # in der Sperrliste


def test_suche_rangfolge_wortanfang_vor_teilwort_flach_vor_tief(welt):
    from scanner.ablage import suche
    _dated_projekt(welt)
    t = suche.suche_ordner(welt["conn"], welt["pid"], "bhs")
    assert [x["name"] for x in t][:3] == ["260601_BHS Start", "260615_BHS Fassade", "260701_BHS Budget"]
    t = suche.suche_ordner(welt["conn"], welt["pid"], "genieur")    # nur als Wortteil
    assert t and t[0]["rel"].endswith("31_Bauingenieur")


def test_suche_endpoint(welt):
    _dated_projekt(welt)
    token = _drop(welt, _datei(welt, "211_Protokoll BHS 9.txt"))
    r = welt["client"].get(f"/api/ablage/{token}/suche", params={"projekt": welt["pid"], "q": "fassade"})
    assert r.status_code == 200 and r.json()["ok"]
    assert any(t["pfad"].endswith("e_Grundlagen/Fassadenplaner") for t in r.json()["treffer"])
    assert welt["client"].get("/api/ablage/gibtesnichtabcdef/suche", params={"projekt": 1, "q": "x"}).status_code == 404


def test_suche_cache_folgt_neuen_ordnern(welt):
    from scanner.ablage import suche
    _dated_projekt(welt)
    assert suche.suche_ordner(welt["conn"], welt["pid"], "neuthema") == []
    (welt["projekt"] / "c_Protokolle" / "260801_Neuthema").mkdir()
    scan_project(welt["pid"], welt["projekt"])
    assert [t["name"] for t in suche.suche_ordner(welt["conn"], welt["pid"], "neuthema")] == ["260801_Neuthema"]


# ── Neuer datierter Ordner ─────────────────────────────────────────────────────

def test_neuer_ordner_bei_mehrheitlich_datierten_unterordnern(welt):
    from datetime import datetime
    from scanner.ablage import ordner as od
    _dated_projekt(welt)
    ctx = kontext.holen(welt["conn"])
    prot = next(o for o in ctx.ordner.values() if o.rel_path == "c_Protokolle")
    res = od.neue_ordner_vorschlaege(ctx, [prot.id], "250918_Kardexabschlüsse Druckmessung Fassade MAIL.pdf",
                                     heute=datetime(2026, 9, 18))
    assert len(res) == 1
    n = res[0]
    assert n["eltern_rel"] == "c_Protokolle" and n["format"] == "JJMMTT" and n["trenner"] == "_"
    assert n["praefix"] == "260918" and n["anteil_datiert"] >= 0.6
    assert n["thema"] == "Kardexabschlüsse Druckmessung Fassade"      # Wörter des Dateinamens, ohne Datum und „MAIL"
    assert n["beispiel"] == "260710_FPS Haustechnik"


def test_neuer_ordner_liegt_die_option_in_einem_datierten_ordner_ist_es_der_elternordner(welt):
    from scanner.ablage import ordner as od
    _dated_projekt(welt)
    ctx = kontext.holen(welt["conn"])
    blatt = next(o for o in ctx.ordner.values() if o.rel_path == "c_Protokolle/260615_BHS Fassade")
    res = od.neue_ordner_vorschlaege(ctx, [blatt.id], "x.pdf")
    assert [n["eltern_rel"] for n in res] == ["c_Protokolle"]


def test_kein_neuer_ordner_ohne_datierte_geschwister_oder_mit_zu_wenigen(welt):
    from scanner.ablage import ordner as od
    _dated_projekt(welt)
    ctx = kontext.holen(welt["conn"])
    for rel in ("31_Vorprojekt/31d_Fachplaner", "51_Ausfuehrung", "e_Grundlagen"):
        o = next(o for o in ctx.ordner.values() if o.rel_path == rel)
        assert od.neue_ordner_vorschlaege(ctx, [o.id], "x.pdf") == []


def test_neuer_ordner_format_acht_stellig_und_leerzeichen(welt):
    from datetime import datetime
    from scanner.ablage import ordner as od
    p = welt["projekt"] / "Sitzungen"
    for n in ("20260101 Eins", "20260201 Zwei", "20260301 Drei", "Allgemein"):
        (p / n).mkdir(parents=True)
    scan_project(welt["pid"], welt["projekt"])
    kontext.invalidieren()
    ctx = kontext.holen(welt["conn"])
    o = next(o for o in ctx.ordner.values() if o.rel_path == "Sitzungen")
    n = od.neue_ordner_vorschlaege(ctx, [o.id], "x.pdf", heute=datetime(2026, 9, 5))[0]
    assert n["format"] == "JJJJMMTT" and n["trenner"] == " " and n["praefix"] == "20260905"


def test_neuer_ordner_ganzer_ablauf_legt_den_ordner_an_und_protokolliert(welt):
    _dated_projekt(welt)
    p = _datei(welt, "Protokoll BHS Dachaufsicht.txt")
    token = _drop(welt, p)
    ziel = welt["projekt"] / "c_Protokolle" / "260918_Dachaufsicht"
    r = _bestaetigen(welt, token, ziel, quelle="neuer_ordner", seite_ms=4200)
    assert r.status_code == 200
    code, res = _ausfuehren(welt, token)
    assert code == 200 and (ziel / p.name).is_file()
    z = welt["conn"].execute("SELECT quelle, gewaehlt_rang, seite_ms, gewaehlt_pfad FROM ablage_log").fetchone()
    assert (z["quelle"], z["gewaehlt_rang"], z["seite_ms"]) == ("neuer_ordner", 0, 4200)


def test_seite_zeigt_suchfeld_neue_ordner_karte_und_zuletzt(welt):
    _dated_projekt(welt)
    c = welt["conn"]
    # Der Rechner „mac-1" hat zuletzt hier abgelegt
    ziel = welt["projekt"] / "e_Grundlagen" / "Fassadenplaner"
    c.execute("INSERT INTO ablage_log (ts, host, dateiname, gewaehlt_pfad, gewaehlt_rang, quelle) "
              "VALUES (strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'mac-1', 'a.pdf', ?, 1, 'option')", (str(ziel),))
    c.commit()
    token = _drop(welt, _datei(welt, "211_Protokoll BHS 9.txt"))
    html = _seite(welt, token)
    assert 'class="ad-suche"' in html and f'data-projekt="{welt["pid"]}"' in html
    assert "Zuletzt verwendet" in html and "e_Grundlagen › Fassadenplaner" in html
    assert "ad-neukarte" in html and "Neuer Ordner" in html and 'class="ad-neu-thema"' in html


def test_zuletzt_verwendet_nur_dieser_host_14_tage_projekt_und_ohne_doppelte(welt):
    c = welt["conn"]
    basis = str(welt["projekt"])

    def log(host, rel, tage=0):
        c.execute("INSERT INTO ablage_log (ts, host, dateiname, gewaehlt_pfad) VALUES "
                  "(strftime('%Y-%m-%dT%H:%M:%SZ','now', ?), ?, 'x', ?)", (f"-{tage} day", host, rel))
    for i in range(7):
        log("mac-1", f"{basis}/c_Protokolle/Ordner{i}")
    log("mac-1", f"{basis}/c_Protokolle/Ordner6")                 # doppelt
    log("mac-1", f"{basis}/c_Protokolle/Alt", tage=20)             # zu alt
    log("mac-2", f"{basis}/c_Protokolle/Fremder")                  # anderer Rechner
    log("mac-1", str(welt["fremd"] / "x"))                          # anderes Projekt
    c.commit()
    z = vorgang.zuletzt_verwendet(c, "mac-1", basis)
    assert len(z) == 5 and z[0]["pfad"].endswith("Ordner6")        # neueste zuerst, höchstens 5
    assert all("Alt" not in x["pfad"] and "Fremder" not in x["pfad"] for x in z)
    assert z[0]["rel"] == "c_Protokolle › Ordner6"
    assert vorgang.zuletzt_verwendet(c, None, basis) == [] and vorgang.zuletzt_verwendet(c, "mac-9", basis) == []


# ── Protokoll: Quelle der Wahl und Zeit ────────────────────────────────────────

@pytest.mark.parametrize("quelle,index,erwartet", [("option", 1, 2), ("suche", None, 0), ("browser", None, 0),
                                                    ("neuer_ordner", None, 0), ("zuletzt", None, None)])
def test_log_kennzeichen_und_rang(welt, quelle, index, erwartet):
    _struktur_scannen(welt)
    token = _drop(welt, _datei(welt, "211_Protokoll BHS 9.txt"))
    extra = {"option_index": index} if index is not None else {}
    assert _bestaetigen(welt, token, welt["projekt"] / "c_Protokolle", quelle=quelle, seite_ms=1234, **extra).status_code == 200
    _ausfuehren(welt, token)
    z = welt["conn"].execute("SELECT quelle, gewaehlt_rang, seite_ms FROM ablage_log").fetchone()
    assert z["quelle"] == quelle and z["seite_ms"] == 1234
    if erwartet is not None:
        assert z["gewaehlt_rang"] == erwartet


def test_log_unbekannte_quelle_und_unsinnige_zeit_werden_abgefangen(welt):
    token = _drop(welt, _datei(welt))
    assert _bestaetigen(welt, token, welt["projekt"] / "c_Protokolle", quelle="hacker", seite_ms="abc").status_code == 200
    _ausfuehren(welt, token)
    z = welt["conn"].execute("SELECT quelle, seite_ms FROM ablage_log").fetchone()
    assert z["quelle"] == "browser" and z["seite_ms"] is None


def test_migration_035(tmp_db):
    spalten = {r[1] for r in tmp_db.execute("PRAGMA table_info(ablage_log)")}
    assert {"quelle", "seite_ms"} <= spalten
    assert tmp_db.execute("SELECT 1 FROM _migrations WHERE id='035_ablage_log_quelle'").fetchone()


# ── Inkrementelle Statistik nach jeder Ablage (Etappe 8, reduziert) ────────────

def _stat(conn, ebene, key, merkmal):
    r = conn.execute("SELECT gewicht FROM ablage_stats WHERE ebene = ? AND key_id = ? AND merkmal = ?",
                     (ebene, key, merkmal)).fetchone()
    return r[0] if r else 0.0


def _ordner_id(conn, path):
    return conn.execute("SELECT id FROM ablage_ordner WHERE path = ?", (str(path),)).fetchone()[0]


def _ablegen(w, name, ziel, **extra):
    p = _datei(w, name, "inhalt " + name)
    token = _drop(w, p)
    assert _bestaetigen(w, token, ziel, **extra).status_code == 200
    code, res = _ausfuehren(w, token)
    assert code == 200
    return token, res


def test_ablage_zaehlt_sofort_in_der_statistik(welt):
    from scanner.ablage import index
    _dated_projekt(welt)
    ziel = welt["projekt"] / "e_Grundlagen" / "Fassadenplaner"
    oid = _ordner_id(welt["conn"], ziel)
    schluessel = index.rolle_key("fassadenplaner")
    vorher_ctx = kontext.holen(welt["conn"])
    n_ctx = vorher_ctx.stats.get(("ordner", oid), {}).get("_n", 0.0)
    anzahl = welt["conn"].execute("SELECT datei_anzahl FROM ablage_ordner WHERE id = ?", (oid,)).fetchone()[0]
    _ablegen(welt, "Sonderthema Gutachten.txt", ziel)
    c = welt["conn"]
    for ebene, key in (("ordner", oid), ("rolle", schluessel), ("global", 0)):
        assert _stat(c, ebene, key, "_n") == 1.0 and _stat(c, ebene, key, "tok:sonderthema") == 1.0
        assert _stat(c, ebene, key, "ext:.txt") == 1.0
    assert c.execute("SELECT datei_anzahl FROM ablage_ordner WHERE id = ?", (oid,)).fetchone()[0] == anzahl + 1
    # auch der Speicher-Cache ist sofort aktuell, ohne Neuladen
    assert kontext.holen(c) is vorher_ctx
    assert vorher_ctx.stats[("ordner", oid)]["_n"] == n_ctx + 1 and vorher_ctx.stats[("ordner", oid)]["tok:sonderthema"] == 1.0
    assert vorher_ctx.ordner[oid].datei_anzahl == anzahl + 1


def test_naechste_aehnliche_datei_wird_nach_dem_lernen_besser_bewertet(welt):
    from scanner.ablage import merkmale as mk
    from scanner.ablage import ordner as od
    from scanner.ablage.kontext import Parameter
    _dated_projekt(welt)
    ziel = welt["projekt"] / "e_Grundlagen" / "Fassadenplaner"
    f = mk.merkmale("Sonderthema Neu.txt")
    ctx = kontext.holen(welt["conn"])
    kands, scores = od.bewerten(ctx, welt["pid"], f, Parameter())
    i = next(i for i, k in enumerate(kands) if k.pfad == str(ziel))
    rang_vorher = sorted(range(len(scores)), key=lambda j: -scores[j]).index(i)
    for n in range(3):
        _ablegen(welt, f"Sonderthema Bericht {n}.txt", ziel)
    assert kontext.holen(welt["conn"]) is ctx                      # derselbe Cache, nicht neu geladen
    kands, scores = od.bewerten(ctx, welt["pid"], f, Parameter())
    i = next(i for i, k in enumerate(kands) if k.pfad == str(ziel))
    beste = max(range(len(scores)), key=lambda j: scores[j])
    assert beste == i and sorted(range(len(scores)), key=lambda j: -scores[j]).index(i) == 0 <= rang_vorher


def test_rueckgaengig_nimmt_die_statistik_zurueck(welt):
    _dated_projekt(welt)
    c = welt["conn"]
    ziel = welt["projekt"] / "e_Grundlagen" / "Fassadenplaner"
    oid = _ordner_id(c, ziel)
    ctx = kontext.holen(c)
    anzahl = c.execute("SELECT datei_anzahl FROM ablage_ordner WHERE id = ?", (oid,)).fetchone()[0]
    vorher = dict(ctx.stats.get(("ordner", oid), {}))
    token, res = _ablegen(welt, "Sonderthema Gutachten.txt", ziel)
    assert _stat(c, "ordner", oid, "tok:sonderthema") == 1.0
    assert at.rueckgaengig(token, welt["reg"], _Sitzung.BASIS, session=welt["sitzung"])[0] == 200
    assert _stat(c, "ordner", oid, "tok:sonderthema") == 0.0 and _stat(c, "global", 0, "_n") == 0.0
    assert c.execute("SELECT datei_anzahl FROM ablage_ordner WHERE id = ?", (oid,)).fetchone()[0] == anzahl
    assert ctx.stats[("ordner", oid)].get("tok:sonderthema", 0.0) == 0.0
    assert ctx.stats[("ordner", oid)].get("_n", 0.0) == vorher.get("_n", 0.0)


def test_neuer_ordner_wird_erfasst_und_gelernt(welt):
    _dated_projekt(welt)
    c = welt["conn"]
    ctx = kontext.holen(c)
    ziel = welt["projekt"] / "c_Protokolle" / "260918_Dachaufsicht"
    _ablegen(welt, "Protokoll BHS Dachaufsicht.txt", ziel)
    r = c.execute("SELECT id, parent_id, art, label, depth, datei_anzahl, slot_id FROM ablage_ordner WHERE path = ?",
                  (str(ziel),)).fetchone()
    assert r is not None and r["art"] == "normal" and r["label"] == "260918 dachaufsicht" and r["datei_anzahl"] == 1
    assert r["parent_id"] == _ordner_id(c, welt["projekt"] / "c_Protokolle") and r["depth"] == 2
    assert _stat(c, "ordner", r["id"], "tok:dachaufsicht") == 1.0
    neu = kontext.holen(c)                                  # Struktur geändert → neuer Kontext mit dem neuen Ordner
    assert neu is not ctx and any(o.path == str(ziel) for o in neu.ordner.values())
    from scanner.ablage import suche
    assert [t["name"] for t in suche.suche_ordner(c, welt["pid"], "dachaufsicht")] == ["260918_Dachaufsicht"]


def test_ablage_in_archivordner_zaehlt_halb_fuer_den_eltern(welt):
    _dated_projekt(welt)
    c = welt["conn"]
    eltern = welt["projekt"] / "31_Vorprojekt" / "31d_Fachplaner" / "31_Bauingenieur"
    _ablegen(welt, "Alter Bericht.txt", eltern / "z_Archiv")
    assert _stat(c, "ordner", _ordner_id(c, eltern), "_n") == 0.5
    assert _stat(c, "ordner", _ordner_id(c, eltern / "z_Archiv"), "_n") == 0.0


def test_kopie_zaehlt_trockenlauf_nicht(welt, monkeypatch):
    _dated_projekt(welt)
    c = welt["conn"]
    ziel = welt["projekt"] / "e_Grundlagen" / "Fassadenplaner"
    _ablegen(welt, "Kopie Datei.txt", ziel, kopie=True)
    assert _stat(c, "global", 0, "_n") == 1.0
    p = _datei(welt, "Trocken Datei.txt")
    token = _drop(welt, p)
    _bestaetigen(welt, token, ziel)
    monkeypatch.setenv("ARCHIVIO_UPLOAD_DRY_RUN", "1")
    _ausfuehren(welt, token)
    assert _stat(c, "global", 0, "_n") == 1.0 and _stat(c, "ordner", _ordner_id(c, ziel), "tok:trocken") == 0.0
