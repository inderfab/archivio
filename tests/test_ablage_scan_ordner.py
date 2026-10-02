import json
import unicodedata

from db import queries
from scanner.walker import scan_project


def _baum(root):
    plan = root / "51_Ausfuehrung" / "51a_Planstände"
    (plan / "z_Archiv").mkdir(parents=True)
    (root / "51_Ausfuehrung" / "51b_Berechnungen_Listen").mkdir()      # leer
    (root / "0-------").mkdir()
    (root / "00_ungueltig").mkdir()
    (root / "201_209_Baugrubenaushub").mkdir()
    (root / "Upload").mkdir()                                         # in der Test-Config ausgeschlossen
    (root / "Upload" / "tief").mkdir()                                # darf nicht erfasst werden
    (root / ".versteckt").mkdir()
    (plan / "GR_EG.txt").write_text("Grundriss", encoding="utf-8")
    (plan / "SN_A.txt").write_text("Schnitt", encoding="utf-8")


def _ordner(conn, project_id):
    rows = conn.execute("SELECT * FROM ablage_ordner WHERE project_id = ?", (project_id,)).fetchall()
    return {r["rel_path"]: r for r in rows}


def _scan(conn, tmp_path, name="Projekt"):
    root = tmp_path / "scan"
    root.mkdir(exist_ok=True)
    _baum(root)
    pid = queries.insert_project(conn, name, str(root))
    conn.commit()
    scan_project(pid, root)
    return pid, root


def test_migrationen_030_031_vorhanden(tmp_db):
    ids = {r[0] for r in tmp_db.execute("SELECT id FROM _migrations")}
    assert {"032_ablage_ordner", "033_ablage_slot"} <= ids
    spalten = {r[1] for r in tmp_db.execute("PRAGMA table_info(ablage_ordner)")}
    assert {"project_id", "path", "parent_id", "rel_path", "depth", "label", "praefix",
            "codes", "slot_id", "art", "datei_anzahl", "letzte_aenderung",
            "zuletzt_gesehen"} <= spalten
    assert {r[1] for r in tmp_db.execute("PRAGMA table_info(ablage_slot)")} >= {
        "label_pfad", "rolle", "anzeige", "abdeckung", "aus_vorlage"}


def test_leere_und_ausgeschlossene_ordner_erfasst(tmp_db, tmp_path):
    pid, root = _scan(tmp_db, tmp_path)
    o = _ordner(tmp_db, pid)

    assert o["51_Ausfuehrung/51b_Berechnungen_Listen"]["datei_anzahl"] == 0    # leer, aber da
    assert o["51_Ausfuehrung/51a_Planstände"]["datei_anzahl"] == 2
    assert o["51_Ausfuehrung/51a_Planstände"]["label"] == "planstaende"
    assert o["51_Ausfuehrung/51a_Planstände"]["praefix"] == "51a"
    assert o["51_Ausfuehrung/51a_Planstände"]["depth"] == 2
    assert o["51_Ausfuehrung/51a_Planstände"]["letzte_aenderung"]
    assert o["51_Ausfuehrung/51a_Planstände/z_Archiv"]["art"] == "archiv"
    assert o["0-------"]["art"] == "trenner"
    assert o["00_ungueltig"]["art"] == "normal"
    assert json.loads(o["201_209_Baugrubenaushub"]["codes"]) == ["201-209"]
    # Ausschluss ist eine eigene Spalte, die Art bleibt erhalten
    assert o["Upload"]["ausgeschlossen"] == 1 and o["Upload"]["art"] == "normal"
    assert o["51_Ausfuehrung/51a_Planstände"]["ausgeschlossen"] == 0

    # nicht betreten und nicht erfasst: Inhalt ausgeschlossener sowie versteckte Ordner
    assert "Upload/tief" not in o
    assert ".versteckt" not in o


def test_eltern_kind_beziehung(tmp_db, tmp_path):
    pid, _ = _scan(tmp_db, tmp_path)
    o = _ordner(tmp_db, pid)
    plan, phase = o["51_Ausfuehrung/51a_Planstände"], o["51_Ausfuehrung"]
    assert plan["parent_id"] == phase["id"]
    assert phase["parent_id"] is None
    assert o["51_Ausfuehrung/51a_Planstände/z_Archiv"]["parent_id"] == plan["id"]


def test_pfad_ist_nfc_auch_bei_nfd_ordner(tmp_db, tmp_path):
    root = tmp_path / "scan"
    (root / unicodedata.normalize("NFD", "Planstände")).mkdir(parents=True)
    pid = queries.insert_project(tmp_db, "P", str(root))
    tmp_db.commit()
    scan_project(pid, root)
    r = tmp_db.execute("SELECT path, name FROM ablage_ordner WHERE project_id = ?", (pid,)).fetchone()
    assert r["path"] == unicodedata.normalize("NFC", r["path"])
    assert r["path"].endswith("Planstände")


def test_umbenennen_entfernt_alten_eintrag(tmp_db, tmp_path):
    pid, root = _scan(tmp_db, tmp_path)
    (root / "51_Ausfuehrung" / "51b_Berechnungen_Listen").rename(
        root / "51_Ausfuehrung" / "51b_Berechnungen")
    scan_project(pid, root)
    o = _ordner(tmp_db, pid)
    assert "51_Ausfuehrung/51b_Berechnungen_Listen" not in o
    assert "51_Ausfuehrung/51b_Berechnungen" in o


def test_geloeschter_ordner_samt_kindern_weg(tmp_db, tmp_path):
    import shutil
    pid, root = _scan(tmp_db, tmp_path)
    shutil.rmtree(root / "51_Ausfuehrung")
    scan_project(pid, root)
    o = _ordner(tmp_db, pid)
    assert not any(k.startswith("51_Ausfuehrung") for k in o)
    assert "0-------" in o


def test_rescan_ist_idempotent_und_behaelt_ids(tmp_db, tmp_path):
    pid, root = _scan(tmp_db, tmp_path)
    vorher = {k: v["id"] for k, v in _ordner(tmp_db, pid).items()}
    scan_project(pid, root)
    nachher = {k: v["id"] for k, v in _ordner(tmp_db, pid).items()}
    assert vorher == nachher


def test_dateizaehler_folgt_neuen_dateien(tmp_db, tmp_path):
    pid, root = _scan(tmp_db, tmp_path)
    (root / "51_Ausfuehrung" / "51b_Berechnungen_Listen" / "k.txt").write_text("x", encoding="utf-8")
    scan_project(pid, root)
    assert _ordner(tmp_db, pid)["51_Ausfuehrung/51b_Berechnungen_Listen"]["datei_anzahl"] == 1


def test_projekt_loeschen_kaskadiert(tmp_db, tmp_path):
    pid, _ = _scan(tmp_db, tmp_path)
    assert _ordner(tmp_db, pid)
    tmp_db.execute("DELETE FROM projects WHERE id = ?", (pid,))
    tmp_db.commit()
    assert tmp_db.execute("SELECT COUNT(*) FROM ablage_ordner").fetchone()[0] == 0


def test_abgebrochener_scan_laesst_struktur_unveraendert(tmp_db, tmp_path):
    pid, root = _scan(tmp_db, tmp_path)
    vorher = set(_ordner(tmp_db, pid))
    import shutil
    shutil.rmtree(root / "51_Ausfuehrung")
    (root / "neu.txt").write_text("x", encoding="utf-8")        # der Abbruch greift bei der ersten Datei
    scan_project(pid, root, cancel_flag={"cancel": True})   # bricht bei der ersten Datei ab
    assert set(_ordner(tmp_db, pid)) == vorher


def test_archiv_labels_aus_config(tmp_db, tmp_path, monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "_settings", {**settings._load(), "ablage": {"archiv_labels": ["Ablage Alt"]}})
    root = tmp_path / "scan"
    (root / "Ablage_Alt").mkdir(parents=True)
    (root / "z_Archiv").mkdir()
    (root / "Ablage_Alt" / "a.txt").write_text("a", encoding="utf-8")
    (root / "z_Archiv" / "b.txt").write_text("b", encoding="utf-8")
    pid = queries.insert_project(tmp_db, "P", str(root))
    tmp_db.commit()
    scan_project(pid, root)
    o = _ordner(tmp_db, pid)
    assert o["Ablage_Alt"]["art"] == "archiv"
    assert o["z_Archiv"]["art"] == "normal"


def test_ausgeschlossenes_archiv_bleibt_archiv(tmp_db, tmp_path):
    """Die Test-Config sperrt „Archiv": der Ordner wird nicht gescannt, ist aber weiterhin ein Archiv-Ordner."""
    root = tmp_path / "scan"
    (root / "c_Protokolle" / "Archiv").mkdir(parents=True)
    (root / "c_Protokolle" / "z_Archiv").mkdir()
    (root / "c_Protokolle" / "a.txt").write_text("x", encoding="utf-8")
    pid = queries.insert_project(tmp_db, "P", str(root))
    tmp_db.commit()
    scan_project(pid, root)
    o = _ordner(tmp_db, pid)
    assert o["c_Protokolle/Archiv"]["art"] == "archiv" and o["c_Protokolle/Archiv"]["ausgeschlossen"] == 1
    assert o["c_Protokolle/z_Archiv"]["art"] == "archiv" and o["c_Protokolle/z_Archiv"]["ausgeschlossen"] == 0


def test_migration_034_stellt_alte_zeilen_um(tmp_db):
    from db import migrations
    pid = tmp_db.execute("INSERT INTO projects (name, path) VALUES ('P', '/p')").lastrowid
    for name, label in (("Upload", "upload"), ("Archiv", "archiv")):
        tmp_db.execute(
            "INSERT INTO ablage_ordner (project_id, path, rel_path, depth, name, label, art, zuletzt_gesehen)"
            " VALUES (?,?,?,1,?,?,'ausgeschlossen','x')", (pid, f"/p/{name}", name, name, label))
    tmp_db.commit()
    migrations._m036(tmp_db)
    zeilen = {r["name"]: (r["art"], r["ausgeschlossen"]) for r in tmp_db.execute("SELECT name, art, ausgeschlossen FROM ablage_ordner")}
    assert zeilen == {"Upload": ("normal", 1), "Archiv": ("archiv", 1)}
    migrations._m036(tmp_db)                                    # idempotent
    assert tmp_db.execute("SELECT 1 FROM _migrations WHERE id='036_ablage_ordner_ausgeschlossen'").fetchone()


# ── Erst-Erfassung nach dem Update (nur Ordner) ────────────────────────────────

def test_erst_erfassung_nur_ordner_ohne_dateien_zu_lesen(tmp_db, tmp_path, monkeypatch):
    import os
    from web import dashboard
    root = tmp_path / "scan"
    _baum(root)
    root.mkdir(exist_ok=True)
    pid = queries.insert_project(tmp_db, "Projekt", str(root))
    tmp_db.commit()
    gelesen = []
    echtes_stat = os.stat
    monkeypatch.setattr(os, "stat", lambda p, *a, **k: (gelesen.append(str(p)), echtes_stat(p, *a, **k))[1])
    erg = dashboard._ablage_erst_erfassung()
    monkeypatch.setattr(os, "stat", echtes_stat)
    assert erg["projekte"] == 1 and erg["ordner"] >= 8
    o = _ordner(tmp_db, pid)
    assert o["51_Ausfuehrung/51a_Planstände"]["datei_anzahl"] == 2          # Namen gezählt
    assert o["51_Ausfuehrung/51a_Planstände"]["letzte_aenderung"] is None     # aber nichts gestat
    assert o["Upload"]["ausgeschlossen"] == 1 and "Upload/tief" not in o and ".versteckt" not in o
    assert o["51_Ausfuehrung/51a_Planstände/z_Archiv"]["art"] == "archiv"
    assert tmp_db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0   # keine Datei gelesen/indexiert
    assert not [g for g in gelesen if g.endswith(".txt")]                         # kein stat() auf Dateien


def test_erst_erfassung_einmalig_und_idempotent(tmp_db, tmp_path):
    from web import dashboard
    root = tmp_path / "scan"
    _baum(root)
    root.mkdir(exist_ok=True)
    pid = queries.insert_project(tmp_db, "Projekt", str(root))
    tmp_db.commit()
    assert dashboard._ablage_erst_erfassung()["projekte"] == 1
    assert tmp_db.execute("SELECT 1 FROM _migrations WHERE id = ?", (f"ablage_ordner_erfasst_{pid}",)).fetchone()
    vorher = {k: v["id"] for k, v in _ordner(tmp_db, pid).items()}
    assert dashboard._ablage_erst_erfassung() == {"projekte": 0, "ordner": 0, "uebersprungen": 0}
    assert {k: v["id"] for k, v in _ordner(tmp_db, pid).items()} == vorher


def test_erst_erfassung_ueberspringt_projekt_mit_ordnern_und_nicht_erreichbares(tmp_db, tmp_path):
    from web import dashboard
    # a) hat schon Ordner (ein Scan war schneller): nicht nochmals, aber als erledigt markiert
    pid, root = _scan(tmp_db, tmp_path)
    # b) Pfad nicht erreichbar (NAS nicht gemountet): übersprungen, keine Marke → nächster Start versucht es nochmals
    weg = queries.insert_project(tmp_db, "Weg", str(tmp_path / "nicht_da"))
    tmp_db.commit()
    erg = dashboard._ablage_erst_erfassung()
    assert erg == {"projekte": 0, "ordner": 0, "uebersprungen": 1}
    assert tmp_db.execute("SELECT 1 FROM _migrations WHERE id = ?", (f"ablage_ordner_erfasst_{pid}",)).fetchone()
    assert not tmp_db.execute("SELECT 1 FROM _migrations WHERE id = ?", (f"ablage_ordner_erfasst_{weg}",)).fetchone()
    assert _ordner(tmp_db, weg) == {}


def test_erst_erfassung_mailbox_und_musterordner_nie(tmp_db, tmp_path, monkeypatch):
    from config import settings
    from web import dashboard
    muster = tmp_path / "000 Musterordner Objekt"
    (muster / "a").mkdir(parents=True)
    monkeypatch.setattr(settings, "_settings", {**settings._load(), "ablage": {"musterordner": str(muster)}})
    tmp_db.execute("INSERT INTO projects (name, path) VALUES ('Muster', ?)", (str(muster),))
    tmp_db.execute("INSERT INTO projects (name, path) VALUES ('Postfach', 'mailbox:Posteingang')")
    tmp_db.commit()
    assert dashboard._ablage_erst_erfassung() == {"projekte": 0, "ordner": 0, "uebersprungen": 0}
    assert tmp_db.execute("SELECT COUNT(*) FROM ablage_ordner").fetchone()[0] == 0


def test_erst_erfassung_nummerierte_projekte_zuerst_neueste_vorn(tmp_db, tmp_path, monkeypatch):
    from scanner.ablage import erfassung
    from web import dashboard
    reihenfolge = []
    echt = erfassung.nur_ordner_erfassen
    monkeypatch.setattr(erfassung, "nur_ordner_erfassen", lambda c, pid, root: (reihenfolge.append(Path(str(root)).name), echt(c, pid, root))[1])
    from pathlib import Path
    for name in ("Ordner 2 Computer", "204 Moosfeld", "215 Flurhof", "182 Bushof", "Ordner 3 Behoerden"):
        d = tmp_path / name
        d.mkdir()
        (d / "a").mkdir()
        queries.insert_project(tmp_db, name, str(d))
    tmp_db.commit()
    dashboard._ablage_erst_erfassung()
    assert reihenfolge == ["215 Flurhof", "204 Moosfeld", "182 Bushof", "Ordner 2 Computer", "Ordner 3 Behoerden"]
