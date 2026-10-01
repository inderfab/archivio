import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from config import settings
from scanner.ablage import index, kontext, vorlage
from scanner.ablage import ordner as od
from scanner.ablage.kontext import Kontext, OrdnerInfo, Parameter, Projekt, Slot
from scanner.ablage.normalisieren import art_bestimmen, zerlege
from scanner.ablage.projekt import DateiInfo
from scanner.ablage.vorschlag import ordner_vorschlag, vorgaenger_im_ordner, vorschlagen

FIXTURE = Path(__file__).parent / "fixtures" / "musterordner_strut.txt"
JETZT = datetime(2026, 10, 1, tzinfo=timezone.utc)

PLAN = "51_Ausfuehrung/51a_Planstände"
CAD = "0 CAD Daten und Export/01_PDF"
BHS = "c_Protokolle/1_Bauherrensitzungen_BHS"
FPS = "c_Protokolle/2_Fachplanersitzungen_FPS"
VERTRAG = "b_Vertraege/1_Fachplaner/1.1_Bauingenieur/01_Vertrag"
BAUMEISTER = "51_Ausfuehrung/51d_Unternehmer/211_Baumeisterarbeiten/02_Ausfuehrung"
GRUNDLAGEN = "e_Grundlagen/1_Plangrundlagen"


def _fixture():
    return FIXTURE.read_text(encoding="utf-8").splitlines()


def _projekt(conn, name, files, alle_ordner=True, hash_prefix=None):
    """Projekt mit Ordnerstruktur der Fixture (leer) plus Dateien je Ordner."""
    pfad = f"/Volumes/Test/{name}"
    pid = conn.execute("INSERT INTO projects (name, path) VALUES (?, ?)", (name, pfad)).lastrowid
    rels = set(files) | (set(_fixture()) if alle_ordner else set())
    for rel in sorted(rels, key=lambda r: r.count("/")):
        parts = rel.split("/")
        for i in range(1, len(parts) + 1):
            sub = "/".join(parts[:i])
            if conn.execute("SELECT 1 FROM ablage_ordner WHERE path=?", (f"{pfad}/{sub}",)).fetchone():
                continue
            n = parts[i - 1]
            z = zerlege(n)
            par = conn.execute("SELECT id FROM ablage_ordner WHERE path=?", (f"{pfad}/{'/'.join(parts[:i-1])}",)).fetchone() \
                if i > 1 else None
            conn.execute(
                "INSERT INTO ablage_ordner (project_id, path, parent_id, rel_path, depth, name, label, praefix,"
                " codes, art, zuletzt_gesehen) VALUES (?,?,?,?,?,?,?,?,?,?,'x')",
                (pid, f"{pfad}/{sub}", par[0] if par else None, sub, i, n, z.label, z.praefix,
                 json.dumps(z.codes), art_bestimmen(n, z.label)))
    k = 0
    for rel, namen in files.items():
        oid = conn.execute("SELECT id FROM ablage_ordner WHERE path=?", (f"{pfad}/{rel}",)).fetchone()[0]
        conn.execute("UPDATE ablage_ordner SET datei_anzahl=?, letzte_aenderung='2026-09-20T00:00:00Z' WHERE id=?",
                     (len(namen), oid))
        for fn in namen:
            k += 1
            did = conn.execute("INSERT INTO documents (project_id, hash, filename, extension, filesize, modified_at)"
                               " VALUES (?,?,?,?,?,?)",
                               (pid, f"{hash_prefix or name}-{k}", fn, "." + fn.rsplit(".", 1)[-1], 10,
                                "2026-09-01T00:00:00Z")).lastrowid
            conn.execute("INSERT INTO document_paths (document_id, path, is_primary) VALUES (?,?,1)",
                         (did, f"{pfad}/{rel}/{fn}"))
    conn.commit()
    return pid


def _dateien(n, nr):
    return {
        PLAN: [f"{nr}_Grundriss EG 1-50 {i}.pdf" for i in range(n)] + [f"{nr}_Schnitt A 1-50 {i}.pdf" for i in range(n // 2)],
        CAD: [f"{nr}_Grundriss OG 1-100 {i}.pdf" for i in range(n // 2)],
        BHS: [f"Protokoll BHS {i}.pdf" for i in range(n)],
        FPS: [f"Protokoll FPS {i}.pdf" for i in range(n)],
        VERTRAG: [f"Werkvertrag Bauingenieur {i}.pdf" for i in range(n)],
        BAUMEISTER: [f"Offerte Baumeisterarbeiten {i}.pdf" for i in range(n)],
        GRUNDLAGEN: [f"Situation Geometer {i}.pdf" for i in range(n)],
    }


@pytest.fixture
def buero(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "_settings", {**settings._load(), "ablage": {}})
    ids = {}
    for nr, name in ((211, "211 Emmenhof Derendingen"), (212, "212 Seepark Mannenbach"), (213, "213 Flurhofstrasse")):
        ids[nr] = _projekt(tmp_db, name, _dateien(30, nr))
    vorlage.neu_berechnen(tmp_db)
    index.neu_aufbauen(tmp_db, JETZT)
    ctx = kontext.lade_kontext(tmp_db, JETZT)
    return tmp_db, ctx, ids


def _pfade(erg):
    return [o["pfad"] for o in erg["optionen"]]


def _ordner_pfad(nr, rel):
    return f"/Volumes/Test/{ {211: '211 Emmenhof Derendingen', 212: '212 Seepark Mannenbach', 213: '213 Flurhofstrasse'}[nr] }/{rel}"


# ── Statistik-Pfad ─────────────────────────────────────────────────────────────

def test_protokoll_bhs_landet_im_richtigen_ordner(buero):
    conn, ctx, ids = buero
    erg = vorschlagen(ctx, DateiInfo("211_Protokoll BHS Nr 99.pdf"), Parameter(), mit_vorgaenger=False)
    assert erg["projekte"][0]["id"] == ids[211]
    assert _pfade(erg)[0] == _ordner_pfad(211, BHS)
    assert erg["optionen"][0]["gruende"]


def test_fps_und_bhs_unterscheiden_sich(buero):
    conn, ctx, ids = buero
    fps = vorschlagen(ctx, DateiInfo("211_Protokoll FPS Nr 99.pdf"), Parameter(), mit_vorgaenger=False)
    assert _pfade(fps)[0] == _ordner_pfad(211, FPS)


def test_vertrag_baumeister_offerte(buero):
    conn, ctx, ids = buero
    e1 = vorschlagen(ctx, DateiInfo("212_Werkvertrag Bauingenieur Nachtrag.pdf"), Parameter(), mit_vorgaenger=False)
    assert e1["projekte"][0]["id"] == ids[212] and _pfade(e1)[0] == _ordner_pfad(212, VERTRAG)
    e2 = vorschlagen(ctx, DateiInfo("213_Offerte Baumeisterarbeiten Rohbau.pdf"), Parameter(), mit_vorgaenger=False)
    ziel = _ordner_pfad(213, BAUMEISTER)
    assert ziel == _pfade(e2)[0] or ziel.startswith(_pfade(e2)[0].rstrip("/") + "/")     # Option darf ein Zweig sein
    assert "Unternehmer" in _pfade(e2)[0]


def test_z_archiv_nie_vorschlag(buero):
    conn, ctx, ids = buero
    for fn in ("211_Protokoll BHS alt.pdf", "211_Grundriss EG 1-50 alt.pdf", "211_Dokument.pdf"):
        erg = vorschlagen(ctx, DateiInfo(fn), Parameter(), mit_vorgaenger=False)
        assert not any(p.rstrip("/").lower().endswith("z_archiv") for p in _pfade(erg)), (fn, _pfade(erg))
    kands = od.kandidaten(ctx, ids[211])
    assert not any(k.rolle == "archiv" for k in kands)


# ── Projekt ────────────────────────────────────────────────────────────────────

def test_projekt_unklar_ohne_signal(buero):
    conn, ctx, ids = buero
    erg = vorschlagen(ctx, DateiInfo("Dokument.pdf"), Parameter())
    assert erg["fall"] == "projekt_unklar"
    assert len(erg["projekte"]) == 3 and erg["optionen"] == []
    assert erg["projekte"][0]["p"] < 0.6


def test_projekt_aus_namen_im_dateinamen(buero):
    conn, ctx, ids = buero
    erg = vorschlagen(ctx, DateiInfo("Emmenhof Besprechung.pdf"), Parameter(), mit_vorgaenger=False)
    assert erg["projekte"][0]["id"] == ids[211]
    assert any("emmenhof" in g.lower() for g in erg["projekte"][0]["gruende"])


def test_projekt_aus_plankopf(buero):
    conn, ctx, ids = buero
    erg = vorschlagen(ctx, DateiInfo("Plan.pdf", text="Projekt 212 Seepark Mannenbach Grundriss"), Parameter(),
                      mit_vorgaenger=False)
    assert erg["projekte"][0]["id"] == ids[212]


def test_projekt_von_hand_erzwungen(buero):
    conn, ctx, ids = buero
    erg = vorschlagen(ctx, DateiInfo("Protokoll BHS 5.pdf"), Parameter(), projekt_id=ids[213], mit_vorgaenger=False)
    assert erg["projekte"][0]["id"] == ids[213] and erg["projekte"][0]["p"] == 1.0
    assert _pfade(erg)[0] == _ordner_pfad(213, BHS)


def test_fachplaner_nummer_ohne_projekt_ignoriert(buero):
    conn, ctx, ids = buero
    erg = vorschlagen(ctx, DateiInfo("3882_Musterstadt_PAW-Elektro.pdf"), Parameter())
    assert erg["fall"] == "projekt_unklar"


# ── Vorgänger, Duplikat, Regel ─────────────────────────────────────────────────

def test_vorgaenger_bonus_standardmaessig_aus(buero):
    conn, ctx, ids = buero
    _projekt_datei(conn, ids[211], "211_Eigenartiger Bericht Fassade.pdf", "h-sonder", "e_Grundlagen/5_Produkte")
    ctx = kontext.lade_kontext(conn, JETZT)
    assert Parameter().vorgaenger_bonus == 0.0
    mit = vorschlagen(ctx, DateiInfo("250918_211_Eigenartiger Bericht Fassade.pdf"), Parameter())
    ohne = vorschlagen(ctx, DateiInfo("250918_211_Eigenartiger Bericht Fassade.pdf"), Parameter(), mit_vorgaenger=False)
    assert [o["pfad"] for o in mit["optionen"]] == [o["pfad"] for o in ohne["optionen"]]      # Ranking unverändert


def test_vorgaenger_bonus_konfigurierbar_mit_rueckfrage_an_der_option(buero):
    conn, ctx, ids = buero
    _projekt_datei(conn, ids[211], "211_Eigenartiger Bericht Fassade.pdf", "h-sonder", "e_Grundlagen/5_Produkte")
    ctx = kontext.lade_kontext(conn, JETZT)
    mit = vorschlagen(ctx, DateiInfo("250918_211_Eigenartiger Bericht Fassade.pdf"), Parameter(vorgaenger_bonus=40))
    erste = next(o for o in mit["optionen"] if o["pfad"].endswith("e_Grundlagen/5_Produkte"))
    assert erste["vorgaenger"].endswith("211_Eigenartiger Bericht Fassade.pdf")
    assert any("Vorgänger" in g for g in erste["gruende"])
    ao = erste["archiv_ordner"]
    assert ao is None or ao.endswith("5_Produkte/z_Archiv")
    assert mit["fall"] != "eindeutig"                      # ein Vorgänger allein macht nie „eindeutig"


def test_rueckfrage_fuer_jeden_gewaehlten_ordner(buero):
    conn, ctx, ids = buero
    _projekt_datei(conn, ids[211], "211_GR_EG.pdf", "h-v1", PLAN)
    ctx = kontext.lade_kontext(conn, JETZT)
    treffer = vorgaenger_im_ordner(ctx, ids[211], "260101_211_GR_EG.pdf", _ordner_pfad(211, PLAN))
    assert treffer and treffer["vorgaenger"].endswith("211_GR_EG.pdf") and treffer["dateiname"] == "211_GR_EG.pdf"
    assert vorgaenger_im_ordner(ctx, ids[211], "260101_211_GR_EG.pdf", _ordner_pfad(211, BHS)) is None
    assert vorgaenger_im_ordner(ctx, ids[211], "211_GR_EG_b.pdf", _ordner_pfad(211, PLAN)) is None     # anderer Name


def test_vorgaenger_nur_bei_gleichem_namen_ausser_datum(buero):
    conn, ctx, ids = buero
    _projekt_datei(conn, ids[211], "211_Bericht Fassade_b.pdf", "h-b", "e_Grundlagen/5_Produkte")
    ctx = kontext.lade_kontext(conn, JETZT)
    # Index _c ist ein anderer Name → kein Vorgänger (Vorgabe: nur das Datum darf abweichen)
    erg = vorschlagen(ctx, DateiInfo("211_Bericht Fassade_c.pdf"), Parameter())
    assert not any("Vorgänger" in g for o in erg["optionen"] for g in o["gruende"])


def test_vorgaenger_in_mehreren_ordnern_ergibt_mehrere_optionen_mit_rueckfrage(buero):
    conn, ctx, ids = buero
    _projekt_datei(conn, ids[211], "211_GR_EG.pdf", "h-v1", PLAN)
    _projekt_datei(conn, ids[211], "211_GR_EG.pdf", "h-v2", BAUMEISTER)
    ctx = kontext.lade_kontext(conn, JETZT)
    erg = vorschlagen(ctx, DateiInfo("260101_211_GR_EG.pdf"), Parameter(vorgaenger_bonus=40))
    mit_rueck = [o for o in erg["optionen"] if o["vorgaenger"]]
    assert len(mit_rueck) >= 1 and all(o["vorgaenger"].endswith("211_GR_EG.pdf") for o in mit_rueck)


def test_vorgaenger_in_vielen_projekten_zaehlt_wenig_fuers_projekt(buero):
    conn, ctx, ids = buero
    for nr in (211, 212, 213):
        _projekt_datei(conn, ids[nr], "image001.png", f"h-img{nr}", "e_Grundlagen/5_Produkte")
    ctx = kontext.lade_kontext(conn, JETZT)
    erg = vorschlagen(ctx, DateiInfo("image001.png"), Parameter())
    assert erg["fall"] == "projekt_unklar"                  # gleichnamig in 3 Projekten: kein Projekt-Signal


def _projekt_datei(conn, pid, filename, h, rel):
    pfad = conn.execute("SELECT path FROM projects WHERE id=?", (pid,)).fetchone()[0]
    if not conn.execute("SELECT 1 FROM ablage_ordner WHERE path=?", (f"{pfad}/{rel}",)).fetchone():
        raise AssertionError(rel)
    did = conn.execute("INSERT INTO documents (project_id, hash, filename, extension, filesize, modified_at)"
                       " VALUES (?,?,?,?,?,?)", (pid, h, filename, "." + filename.rsplit(".", 1)[-1], 1,
                                                  "2026-09-01T00:00:00Z")).lastrowid
    conn.execute("INSERT INTO document_paths (document_id, path, is_primary) VALUES (?,?,1)", (did, f"{pfad}/{rel}/{filename}"))
    conn.commit()


def test_duplikat_erkannt(buero):
    conn, ctx, ids = buero
    h = conn.execute("SELECT hash FROM documents WHERE filename LIKE 'Protokoll BHS 3.pdf' LIMIT 1").fetchone()[0]
    erg = vorschlagen(ctx, DateiInfo("Egal.pdf", hash=h), Parameter())
    assert erg["fall"] == "duplikat"
    assert erg["duplikate"][0]["pfad"].endswith("Protokoll BHS 3.pdf")


def test_explizite_regel(buero):
    conn, ctx, ids = buero
    ctx.regeln = [("doktyp:rechnung", "vertraege")]
    erg = vorschlagen(ctx, DateiInfo("211_Rechnung 4711.pdf"), Parameter(), mit_vorgaenger=False)
    assert erg["fall"] == "eindeutig" and _pfade(erg)[0] == _ordner_pfad(211, "b_Vertraege")
    assert erg["optionen"][0]["gruende"][0].startswith("Regel:")


# ── Hierarchie, Absicht, neue Projekte ─────────────────────────────────────────

def _mini():
    ctx = Kontext(jetzt=JETZT)
    ctx.projekte[1] = Projekt(1, "100 Test", "/p", 100)
    ctx.ordner[1] = OrdnerInfo(1, 1, "/p/A", "A", "A", "a", None, "normal", None)
    ctx.ordner[2] = OrdnerInfo(2, 1, "/p/A/x", "A/x", "x", "x", 1, "normal", None)
    ctx.ordner[3] = OrdnerInfo(3, 1, "/p/A/y", "A/y", "y", "y", 1, "normal", None)
    ctx.ordner[4] = OrdnerInfo(4, 1, "/p/B", "B", "B", "b", None, "normal", None)
    return ctx.fertig()


def test_hierarchie_teilweise():
    ctx = _mini()
    kands = od.kandidaten(ctx, 1)
    p = {1: 0.02, 2: 0.46, 3: 0.44, 4: 0.08}
    h = od.hierarchie(ctx, 1, kands, [p[k.key] for k in kands], Parameter(schwelle_sicher=0.8))
    assert h["fall"] == "teilweise" and h["sicher_pfad"] == "/p/A"
    assert abs(h["sicher_p"] - 0.92) < 1e-9
    assert [k.key for k, _ in h["optionen"]][:2] == [2, 3]


def test_hierarchie_eindeutig_und_unklar():
    ctx = _mini()
    ctx.stats[("ordner", 2)] = {"_n": 5.0}            # „eindeutig" braucht Vorgeschichte im Ordner selbst
    kands = od.kandidaten(ctx, 1)
    e = od.hierarchie(ctx, 1, kands, [{1: 0.0, 2: 0.9, 3: 0.05, 4: 0.05}[k.key] for k in kands], Parameter(schwelle_sicher=0.8))
    assert e["fall"] == "eindeutig" and e["sicher_pfad"] == "/p/A/x"
    ctx2 = _mini()                                     # ohne Vorgeschichte: nur „sicher bis", nie „eindeutig"
    kands2 = od.kandidaten(ctx2, 1)
    e2 = od.hierarchie(ctx2, 1, kands2, [{1: 0.0, 2: 0.9, 3: 0.05, 4: 0.05}[k.key] for k in kands2], Parameter(schwelle_sicher=0.8))
    assert e2["fall"] == "teilweise" and e2["sicher_pfad"] == "/p/A/x"
    u = od.hierarchie(ctx, 1, kands, [{1: 0.05, 2: 0.3, 3: 0.2, 4: 0.45}[k.key] for k in kands], Parameter(schwelle_sicher=0.8))
    assert u["fall"] == "ordner_unklar" and u["sicher_pfad"] == "/p"


def test_teilweise_im_synthetischen_buero(buero):
    conn, ctx, ids = buero
    # BHS und FPS tragen gleich viel Evidenz für „Protokoll" → ohne das Kürzel nur Hierarchie-Konfidenz
    erg = vorschlagen(ctx, DateiInfo("211_Protokoll.pdf"), Parameter(), mit_vorgaenger=False)
    assert erg["fall"] in ("teilweise", "ordner_unklar", "eindeutig")
    assert len(erg["optionen"]) >= 1
    sicher = erg["sicher_bis"]["pfad"]
    assert all(p.startswith(sicher) for p in _pfade(erg))
    if erg["fall"] != "eindeutig":
        # Optionen sind Zweige: BHS/FPS selbst oder ihr gemeinsamer Elternordner „Protokolle"
        ziele = {_ordner_pfad(211, BHS), _ordner_pfad(211, FPS)}
        assert any(z == p or z.startswith(p.rstrip("/") + "/") for z in ziele for p in _pfade(erg))


def test_absicht_optionen_fuer_plan_pdf(buero):
    conn, ctx, ids = buero
    erg = vorschlagen(ctx, DateiInfo("211_Grundriss EG 1-50.pdf"), Parameter(), mit_vorgaenger=False)
    labels = {o["label"] for o in erg["optionen"]}
    assert _ordner_pfad(211, PLAN) in _pfade(erg)
    assert "Phasenstand / Planfreeze" in labels
    if len(erg["optionen"]) > 1:
        assert "Aktueller Stand (CAD-Export)" in labels


def test_leerer_vorlage_ordner_im_neuen_projekt(buero):
    conn, ctx, ids = buero
    # neues Projekt: komplette Vorlage-Struktur, aber nur 3 Dateien
    neu = _projekt(conn, "214 Neubau Test", {BHS: ["Protokoll BHS 1.pdf"], PLAN: ["214_Grundriss.pdf"], FPS: []})
    vorlage.zuordnen(conn, neu)                       # wie nach dem Scan
    ctx = kontext.lade_kontext(conn, JETZT)
    erg = vorschlagen(ctx, DateiInfo("214_Offerte Baumeisterarbeiten Rohbau.pdf"), Parameter(), mit_vorgaenger=False)
    assert erg["projekte"][0]["id"] == neu
    assert _pfade(erg)[0] == _ordner_pfad_neu(BAUMEISTER)      # leerer Ordner, getragen vom Slot


def _ordner_pfad_neu(rel):
    return f"/Volumes/Test/214 Neubau Test/{rel}"


def test_virtueller_slot_fehlender_ordner(buero):
    conn, ctx, ids = buero
    neu = _projekt(conn, "215 Ohne BHS", {PLAN: ["215_Grundriss.pdf"]}, alle_ordner=False)
    # Elternordner existiert, BHS-Ordner fehlt
    pfad = "/Volumes/Test/215 Ohne BHS"
    z = zerlege("c_Protokolle")
    conn.execute("INSERT INTO ablage_ordner (project_id, path, rel_path, depth, name, label, praefix, codes, art,"
                 " zuletzt_gesehen) VALUES (?,?,?,?,?,?,?,?,?,'x')",
                 (neu, f"{pfad}/c_Protokolle", "c_Protokolle", 1, "c_Protokolle", z.label, z.praefix, "[]", "normal"))
    conn.commit()
    vorlage.neu_berechnen(conn)
    index.neu_aufbauen(conn, JETZT)
    ctx = kontext.lade_kontext(conn, JETZT)
    erg = vorschlagen(ctx, DateiInfo("215_Protokoll BHS 7.pdf"), Parameter(), mit_vorgaenger=False)
    erste = erg["optionen"][0]
    assert erste["pfad"].endswith("c_Protokolle/1_Bauherrensitzungen_BHS") and erste["neu_anlegen"] is True


def test_vorschlagspfad_fasst_das_nas_nicht_an(buero, monkeypatch):
    import os
    conn, ctx, ids = buero

    def verboten(*a, **k):
        raise AssertionError("os.walk im Vorschlagspfad")
    monkeypatch.setattr(os, "walk", verboten)
    vorschlagen(ctx, DateiInfo("211_Protokoll BHS 5.pdf"), Parameter())


def test_laufzeit_unter_300ms(buero):
    conn, ctx, ids = buero
    vorschlagen(ctx, DateiInfo("211_Protokoll BHS 5.pdf"), Parameter())          # aufwärmen
    zeiten = []
    for i in range(20):
        t = time.perf_counter()
        vorschlagen(ctx, DateiInfo(f"211_Protokoll BHS {i}.pdf"), Parameter())
        zeiten.append((time.perf_counter() - t) * 1000)
    assert sorted(zeiten)[int(len(zeiten) * 0.95) - 1] < 300


def test_kontext_cache_und_invalidieren(buero):
    conn, ctx, ids = buero
    a = kontext.holen(conn)
    assert kontext.holen(conn) is a
    kontext.invalidieren()
    assert kontext.holen(conn) is not a


# ── Namens-Signal, Teilbaum, Zuordnung nach dem Scan ───────────────────────────

def _namens_ctx():
    ctx = Kontext(jetzt=JETZT)
    ctx.projekte[1] = Projekt(1, "100 Test", "/p", 100)
    ctx.ordner[1] = OrdnerInfo(1, 1, "/p/Unternehmer", "Unternehmer", "Unternehmer", "unternehmer", None, "normal", None)
    ctx.ordner[2] = OrdnerInfo(2, 1, "/p/Unternehmer/260721_Kardex Abschlusswinkel", "x", "260721_Kardex Abschlusswinkel",
                               "kardex abschlusswinkel", 1, "normal", None)
    ctx.ordner[3] = OrdnerInfo(3, 1, "/p/Unternehmer/Metallbau", "y", "Metallbau", "metallbau", 1, "normal", None)
    return ctx.fertig()


def test_namens_treffer_auch_in_zusammensetzungen():
    from scanner.ablage import merkmale as mk
    ctx = _namens_ctx()
    f = mk.merkmale("260729_Kardexabschlüsse - Druckmessung.pdf")
    t = od.namens_treffer(ctx, 1, f)
    assert 2 in t and t[2][0] > 0 and "kardex" in t[2][1]
    assert 3 not in t
    # der Elternordner „Unternehmer" bekommt nichts, aber ein Treffer im Kind überträgt halb auf dessen Kinder
    assert od.namens_treffer(ctx, 1, mk.merkmale("Foto.jpg")) == {}


def test_teilbaum_summiert_unterordner():
    ctx = _namens_ctx()
    ctx.stats[("ordner", 2)] = {"_n": 4.0, "tok:a": 3.0}
    ctx.stats[("ordner", 3)] = {"_n": 2.0, "tok:a": 1.0}
    tb = ctx.teilbaum(1)
    assert tb[1]["_n"] == 6.0 and tb[1]["tok:a"] == 4.0          # Elternordner = Summe der Kinder
    assert tb[2]["_n"] == 4.0


def test_slot_zuordnung_direkt_nach_dem_scan(tmp_db, monkeypatch, tmp_path):
    from db import queries
    from scanner.walker import scan_project
    monkeypatch.setattr(settings, "_settings", {**settings._load(), "ablage": {}})
    # Slots aus drei Fixture-Projekten, danach ein NEUES Projekt scannen → Ordner bekommen sofort einen Slot
    for i in range(3):
        _projekt(tmp_db, f"{211 + i} P", _dateien(5, 211 + i))
    vorlage.neu_berechnen(tmp_db)
    root = tmp_path / "scan" / "51_Ausfuehrung" / "51a_Planstände"
    root.mkdir(parents=True)
    (root / "a.txt").write_text("x", encoding="utf-8")
    pid = queries.insert_project(tmp_db, "299 Neu", str(tmp_path / "scan"))
    tmp_db.commit()
    scan_project(pid, tmp_path / "scan")
    zeilen = {r["rel_path"]: r["slot_id"] for r in tmp_db.execute(
        "SELECT rel_path, slot_id FROM ablage_ordner WHERE project_id = ?", (pid,))}
    assert zeilen["51_Ausfuehrung/51a_Planstände"] is not None and zeilen["51_Ausfuehrung"] is not None
