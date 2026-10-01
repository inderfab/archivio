import json
import random
from pathlib import Path

import pytest

from config import settings
from scanner.ablage import vorlage
from scanner.ablage.normalisieren import art_bestimmen, label_pfad, zerlege

FIXTURE = Path(__file__).parent / "fixtures" / "musterordner_strut.txt"


def _fixture_pfade():
    return FIXTURE.read_text(encoding="utf-8").splitlines()


def _cfg(monkeypatch, **ablage):
    monkeypatch.setattr(settings, "_settings", {**settings._load(), "ablage": ablage})


def _projekt(conn, name, rel_pfade, pfad=None):
    """Legt ein Projekt samt ablage_ordner-Zeilen an (so, wie der Scan sie schreiben würde)."""
    pfad = pfad or f"/Volumes/Test/{name}"
    cur = conn.execute("INSERT INTO projects (name, path) VALUES (?, ?)", (name, pfad))
    pid = cur.lastrowid
    for rel in rel_pfade:
        n = rel.rsplit("/", 1)[-1]
        z = zerlege(n)
        conn.execute(
            "INSERT INTO ablage_ordner (project_id, path, rel_path, depth, name, label, praefix,"
            " codes, art, zuletzt_gesehen) VALUES (?,?,?,?,?,?,?,?,?,'x')",
            (pid, f"{pfad}/{rel}", rel, rel.count("/") + 1, n, z.label, z.praefix,
             json.dumps(z.codes), art_bestimmen(n, z.label)))
    conn.commit()
    return pid


def _abweichend(seed, weg=80):
    rnd = random.Random(seed)
    pfade = _fixture_pfade()
    entf = set(rnd.sample(pfade, weg))
    res = [p for p in pfade if p not in entf]
    res += [f"Spezial_{seed}", f"51_Ausfuehrung/Extra_Ordner_{seed}"]
    return res


@pytest.fixture
def muster_baum(tmp_path):
    root = tmp_path / "000 Musterordner Objekt"
    for p in _fixture_pfade():
        (root / p).mkdir(parents=True, exist_ok=True)
    (root / ".Spotlight-V100").mkdir()
    (root / "0-------").mkdir()
    return root


# ── Weg A ──────────────────────────────────────────────────────────────────────

def test_import_fixture_794_ordner_und_slots(tmp_db, muster_baum):
    res = vorlage.importiere_musterordner(tmp_db, str(muster_baum))
    assert res["ordner"] == 794 + 1          # Fixture + Trenner; der versteckte Ordner zählt nicht
    labels = {r["label_pfad"]: r for r in tmp_db.execute("SELECT * FROM ablage_slot")}
    assert res["slots"] == len(labels)
    assert all(r["aus_vorlage"] == 1 for r in labels.values())
    for erwartet in ("ausfuehrung/planstaende", "ausfuehrung/fachplaner/bauingenieur",
                     "vorprojekt/planstaende/archiv", "cad daten und export/pdf"):
        assert erwartet in labels, erwartet
    # Inkonsistenzen fallen zusammen, es gibt keine Slots mit Umlaut-Variante
    assert not any("planstande" in lp for lp in labels)
    assert labels["ausfuehrung/planstaende"]["anzeige"] == "51a_Planstände"
    assert labels["ausfuehrung/planstaende"]["rolle"] == "planstaende"


def test_import_fehlender_ordner(tmp_db, tmp_path):
    with pytest.raises(FileNotFoundError):
        vorlage.importiere_musterordner(tmp_db, str(tmp_path / "gibt es nicht"))


def test_musterordner_ist_kein_projekt_und_lernt_nicht(tmp_db, monkeypatch):
    muster = "/Volumes/Test/000 Musterordner Objekt"
    _cfg(monkeypatch, musterordner=muster)
    _projekt(tmp_db, "000 Musterordner Objekt", _fixture_pfade(), pfad=muster)
    p1 = _projekt(tmp_db, "211 Emmenhof", _fixture_pfade())
    assert vorlage.ist_musterprojekt(muster + "/")
    assert not vorlage.ist_musterprojekt("/Volumes/Test/211 Emmenhof")
    assert vorlage.lern_projekte(tmp_db) == [p1]


def test_weg_a_gewinnt_und_liefert_abdeckung(tmp_db, muster_baum, monkeypatch):
    _cfg(monkeypatch, musterordner=str(muster_baum))
    vorlage.importiere_musterordner(tmp_db, str(muster_baum))
    _projekt(tmp_db, "211 A", _fixture_pfade())
    _projekt(tmp_db, "212 B", _abweichend(1))
    res = vorlage.neu_berechnen(tmp_db)
    assert res["weg"] == "A"
    slot = tmp_db.execute("SELECT * FROM ablage_slot WHERE label_pfad='ausfuehrung/planstaende'").fetchone()
    assert slot["aus_vorlage"] == 1 and slot["abdeckung"] >= 0.5
    # Projektspezifischer Ordner wird KEIN Slot
    assert tmp_db.execute("SELECT COUNT(*) FROM ablage_slot WHERE label_pfad LIKE '%extra ordner%'"
                          ).fetchone()[0] == 0


def test_vorlage_entfernen(tmp_db, muster_baum):
    vorlage.importiere_musterordner(tmp_db, str(muster_baum))
    vorlage.vorlage_entfernen(tmp_db)
    assert tmp_db.execute("SELECT COUNT(*) FROM ablage_slot").fetchone()[0] == 0


# ── Weg B ──────────────────────────────────────────────────────────────────────

def test_herleitung_findet_vorlage_trotz_abweichungen(tmp_db, monkeypatch):
    _cfg(monkeypatch)
    for i in range(3):
        _projekt(tmp_db, f"{211 + i} P{i}", _abweichend(i))
    res = vorlage.neu_berechnen(tmp_db)
    assert res["weg"] == "B" and res["genutzt"] == 3
    labels = {r["label_pfad"]: r for r in tmp_db.execute("SELECT * FROM ablage_slot")}
    assert "ausfuehrung/planstaende" in labels
    assert labels["ausfuehrung/planstaende"]["anzeige"] == "51a_Planstände"
    assert not any("extra ordner" in lp for lp in labels)       # je Projekt anders → kein Slot
    assert all(r["aus_vorlage"] == 0 for r in labels.values())
    assert len(labels) > 500


def test_herleitung_zusatzordner_in_allen_projekten_ist_slot(tmp_db, monkeypatch):
    _cfg(monkeypatch)
    for i in range(3):
        _projekt(tmp_db, f"{211 + i} P{i}", _fixture_pfade() + ["51_Ausfuehrung/Extra"])
    vorlage.neu_berechnen(tmp_db)
    assert tmp_db.execute("SELECT 1 FROM ablage_slot WHERE label_pfad='ausfuehrung/extra'").fetchone()


def test_altprojekt_mit_fremder_struktur_wird_aussortiert(tmp_db, monkeypatch):
    _cfg(monkeypatch)
    ids = [_projekt(tmp_db, f"{211 + i} P{i}", _abweichend(i)) for i in range(3)]
    fremd = [f"Bereich_{a}/Unter_{b}" for a in range(8) for b in range(5)] + [f"Bereich_{a}" for a in range(8)]
    alt = _projekt(tmp_db, "090 Alt", fremd)
    assert alt in vorlage.lern_projekte(tmp_db)
    res = vorlage.neu_berechnen(tmp_db)
    assert res["genutzt"] == 3 and res["lern_projekte"] == 4
    assert not tmp_db.execute("SELECT 1 FROM ablage_slot WHERE label_pfad LIKE 'bereich%'").fetchone()
    # der Altprojekt-Ordner hat keinen Slot, die normalen schon
    assert tmp_db.execute("SELECT COUNT(*) FROM ablage_ordner WHERE project_id=? AND slot_id IS NOT NULL",
                          (alt,)).fetchone()[0] == 0
    assert tmp_db.execute("SELECT COUNT(*) FROM ablage_ordner WHERE project_id=? AND slot_id IS NOT NULL",
                          (ids[0],)).fetchone()[0] > 500


def test_lernen_ab_projektnummer(tmp_db, monkeypatch):
    _cfg(monkeypatch, lernen_ab_projektnummer=184)
    alt = _projekt(tmp_db, "150 Alt", _fixture_pfade())
    neu = _projekt(tmp_db, "211 Neu", _fixture_pfade())
    ohne = _projekt(tmp_db, "Ohne Nummer", _fixture_pfade())
    assert vorlage.lern_projekte(tmp_db) == [neu]
    _cfg(monkeypatch)
    assert sorted(vorlage.lern_projekte(tmp_db)) == sorted([alt, neu, ohne])


def test_kleine_projekte_lernen_nicht(tmp_db, monkeypatch):
    _cfg(monkeypatch)
    _projekt(tmp_db, "211 Klein", _fixture_pfade()[:10])
    assert vorlage.lern_projekte(tmp_db) == []


def test_projektnummer_regex_konfigurierbar(monkeypatch):
    _cfg(monkeypatch)
    assert vorlage.projekt_nummer("211 Emmenhof") == 211
    assert vorlage.projekt_nummer("Emmenhof") is None
    _cfg(monkeypatch, projektnummer_regex=r"P-(\d+)")
    assert vorlage.projekt_nummer("P-42 Haus") == 42


# ── Zuordnung ──────────────────────────────────────────────────────────────────

def test_slot_zuordnung_inkl_archiv_und_trenner(tmp_db, monkeypatch):
    _cfg(monkeypatch)
    pid = _projekt(tmp_db, "211 A", _fixture_pfade() + ["0-------"])
    for i in range(2):
        _projekt(tmp_db, f"{212 + i} B", _fixture_pfade())
    vorlage.neu_berechnen(tmp_db)
    o = {r["rel_path"]: r for r in tmp_db.execute(
        "SELECT o.rel_path, o.slot_id, s.label_pfad FROM ablage_ordner o "
        "LEFT JOIN ablage_slot s ON s.id = o.slot_id WHERE o.project_id=?", (pid,))}
    assert o["51_Ausfuehrung/51a_Planstände"]["label_pfad"] == "ausfuehrung/planstaende"
    assert o["51_Ausfuehrung/51a_Planstände/z_Archiv"]["label_pfad"] == "ausfuehrung/planstaende/archiv"
    assert o["0-------"]["slot_id"] is None
    # beide Schreibweisen von „in Bearbeitung" teilen den Slot
    assert o["22_Wettbewerb_Studienauftrag"]["slot_id"] is not None


def test_neuberechnung_ist_idempotent(tmp_db, monkeypatch):
    _cfg(monkeypatch)
    for i in range(3):
        _projekt(tmp_db, f"{211 + i} P{i}", _abweichend(i))
    vorlage.neu_berechnen(tmp_db)
    zweite = vorlage.neu_berechnen(tmp_db)
    assert zweite["zugeordnet_geaendert"] == 0


def test_struktur_uebersicht(tmp_db, monkeypatch):
    _cfg(monkeypatch)
    for i in range(3):
        _projekt(tmp_db, f"{211 + i} P{i}", _fixture_pfade())
    tmp_db.execute("UPDATE ablage_ordner SET datei_anzahl = 4 WHERE rel_path = '51_Ausfuehrung/51a_Planstände'")
    tmp_db.commit()
    vorlage.neu_berechnen(tmp_db)
    u = vorlage.struktur_uebersicht(tmp_db)
    assert u["lern_projekte"] == 3 and not u["aus_vorlage"]
    slot = next(s for s in u["slots"] if s["label_pfad"] == "ausfuehrung/planstaende")
    assert slot["abdeckung_pct"] == 100 and slot["dateien"] == 12 and slot["ordner"] == 3
    # Baumreihenfolge: Eltern direkt vor ihren Kindern
    lps = [s["label_pfad"] for s in u["slots"]]
    assert lps.index("ausfuehrung") < lps.index("ausfuehrung/planstaende") < lps.index("ausfuehrung/planstaende/archiv")


# ── Einstellungen (Web) ────────────────────────────────────────────────────────

def _client():
    from fastapi.testclient import TestClient
    from web.main import app
    return TestClient(app, follow_redirects=False)


def test_einstellungsseite_zeigt_erkannte_struktur(tmp_db, monkeypatch):
    _cfg(monkeypatch)
    for i in range(3):
        _projekt(tmp_db, f"{211 + i} P{i}", _fixture_pfade())
    vorlage.neu_berechnen(tmp_db)
    r = _client().get("/dashboard/settings")
    assert r.status_code == 200
    assert "Erkannte Struktur" in r.text and "51a_Planstände" in r.text


def test_einstellungsseite_ohne_ordner_nichts_erkannt(tmp_db):
    r = _client().get("/dashboard/settings")
    assert r.status_code == 200 and "noch nichts erkannt" in r.text


def test_ablage_settings_importiert_muster_und_speichert(tmp_db, muster_baum):
    r = _client().post("/dashboard/ablage-settings",
                       data={"ablage_musterordner": str(muster_baum), "ablage_lernen_ab": "184"})
    assert r.status_code == 303 and "saved=1" in r.headers["location"]
    assert settings.get("ablage.musterordner") == str(muster_baum)
    assert settings.get("ablage.lernen_ab_projektnummer") == 184
    assert tmp_db.execute("SELECT COUNT(*) FROM ablage_slot WHERE aus_vorlage=1").fetchone()[0] > 500

    # Musterordner wieder leeren → Vorlage-Slots weg
    r = _client().post("/dashboard/ablage-settings", data={"ablage_musterordner": "", "ablage_lernen_ab": ""})
    assert "saved=1" in r.headers["location"]
    assert tmp_db.execute("SELECT COUNT(*) FROM ablage_slot WHERE aus_vorlage=1").fetchone()[0] == 0


def test_ablage_settings_fehler(tmp_db, tmp_path):
    r = _client().post("/dashboard/ablage-settings",
                       data={"ablage_musterordner": str(tmp_path / "weg"), "ablage_lernen_ab": ""})
    assert "ablage_fehler" in r.headers["location"] and "saved" not in r.headers["location"]
    r = _client().post("/dashboard/ablage-settings", data={"ablage_musterordner": "", "ablage_lernen_ab": "abc"})
    assert "ablage_fehler" in r.headers["location"]


def test_struktur_neu_berechnen_route(tmp_db, monkeypatch):
    _cfg(monkeypatch)
    for i in range(3):
        _projekt(tmp_db, f"{211 + i} P{i}", _fixture_pfade())
    r = _client().post("/dashboard/ablage-struktur/neu")
    assert r.status_code == 303
    assert tmp_db.execute("SELECT COUNT(*) FROM ablage_slot").fetchone()[0] > 500
