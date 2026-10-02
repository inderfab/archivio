import json
from datetime import datetime, timezone

from config import settings
from scanner.ablage import index, vorlage
from scanner.ablage.normalisieren import art_bestimmen, zerlege

JETZT = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _f(i, name, mod, ordner_id):
    return {"id": i, "filename": name, "filesize": 1, "modified_at": mod, "ordner_id": ordner_id}


ORDNER = {
    1: {"slot_id": 10, "rolle": "planstaende", "art": "normal", "parent_id": None},
    2: {"slot_id": 11, "rolle": "archiv", "art": "archiv", "parent_id": 1},
    3: {"slot_id": 12, "rolle": "vertraege", "art": "normal", "parent_id": None},
    4: {"slot_id": None, "rolle": "x", "art": "ausgeschlossen", "parent_id": None},
}


def test_alters_gewicht_halbwertszeit():
    assert abs(index.alters_gewicht("2026-10-01T00:00:00Z", JETZT) - 1.0) < 1e-9
    assert abs(index.alters_gewicht("2024-10-01T00:00:00Z", JETZT) - 0.5 ** (730 / 730)) < 0.01
    assert index.alters_gewicht("2026-12-01T00:00:00Z", JETZT) == 1.0       # Zukunft: kein Bonus > 1
    assert index.alters_gewicht("2026-05-01T00:00:00+02:00", JETZT) < 1.0   # Zeitzonen-Offset geparst
    assert index.alters_gewicht(None, JETZT) == 0.5


def test_aggregation_vier_ebenen():
    dateien = [_f(i, f"GR_EG_{i}.pdf", "2026-10-01T00:00:00Z", 1) for i in range(5)] + \
              [_f(10 + i, f"Vertrag_{i}.pdf", "2026-10-01T00:00:00Z", 3) for i in range(3)]
    s = index.statistik_berechnen(dateien, ORDNER, jetzt=JETZT, min_gewicht=2)
    assert s[("ordner", 1)]["_n"] == 5 and s[("ordner", 1)]["ext:.pdf"] == 5
    assert s[("ordner", 1)]["plantyp:GR"] == 5
    assert s[("ordner", 3)]["doktyp:vertrag"] == 3
    assert s[("slot", 10)]["plantyp:GR"] == 5 and ("slot", 12) in s
    assert s[("rolle", index.rolle_key("planstaende"))]["_n"] == 5
    g = s[("global", 0)]
    assert g["_n"] == 8 and g["ext:.pdf"] == 8 and g["plantyp:GR"] == 5


def test_seltene_merkmale_fliegen_raus():
    dateien = [_f(i, "Plan.pdf", "2026-10-01T00:00:00Z", 1) for i in range(5)]
    dateien.append(_f(9, "Plan Einzelstueck.pdf", "2026-10-01T00:00:00Z", 1))
    s = index.statistik_berechnen(dateien, ORDNER, jetzt=JETZT, min_gewicht=2)
    assert "tok:einzelstueck" not in s[("global", 0)]
    assert "tok:einzelstueck" not in s[("ordner", 1)]
    assert "tok:plan" in s[("global", 0)]


def test_archiv_zaehlt_halb_fuer_den_elternordner():
    dateien = [_f(1, "Plan A.pdf", "2026-10-01T00:00:00Z", 1),
               _f(2, "Plan A alt.pdf", "2026-10-01T00:00:00Z", 2),
               _f(3, "Plan A alt2.pdf", "2026-10-01T00:00:00Z", 2)]
    s = index.statistik_berechnen(dateien, ORDNER, jetzt=JETZT, min_gewicht=0)
    assert s[("ordner", 1)]["_n"] == 1 + 0.5 + 0.5
    assert ("ordner", 2) not in s                    # der Archivordner selbst bekommt nichts


def test_ausgeschlossen_und_unbekannt_zaehlen_nicht():
    dateien = [_f(1, "a.pdf", "2026-10-01T00:00:00Z", 4), _f(2, "b.pdf", "2026-10-01T00:00:00Z", 99)]
    s = index.statistik_berechnen(dateien, ORDNER, jetzt=JETZT, min_gewicht=0)
    assert s[("global", 0)] == {} or s[("global", 0)].get("_n", 0) == 0


def test_zeit_trennung_nur_vor():
    dateien = [_f(1, "Alt.pdf", "2026-01-01T00:00:00Z", 1), _f(2, "Frisch.pdf", "2026-08-01T00:00:00Z", 1)]
    s = index.statistik_berechnen(dateien, ORDNER, jetzt=JETZT, nur_vor="2026-07-01T00:00:00Z", min_gewicht=0)
    assert "tok:alt" in s[("ordner", 1)] and "tok:frisch" not in s[("ordner", 1)]


def test_alte_dateien_zaehlen_weniger():
    dateien = [_f(1, "Alt.pdf", "2024-10-01T00:00:00Z", 1), _f(2, "Frisch.pdf", "2026-10-01T00:00:00Z", 1)]
    s = index.statistik_berechnen(dateien, ORDNER, jetzt=JETZT, min_gewicht=0)
    assert s[("ordner", 1)]["tok:frisch"] > 0.99 and abs(s[("ordner", 1)]["tok:alt"] - 0.5) < 0.01


# ── Datenbank ──────────────────────────────────────────────────────────────────

def _projekt_mit_dateien(conn, name, ordner_dateien):
    pfad = f"/Volumes/Test/{name}"
    pid = conn.execute("INSERT INTO projects (name, path) VALUES (?, ?)", (name, pfad)).lastrowid
    # genug Ordner, damit das Projekt mitlernt
    rels = set(ordner_dateien) | {f"Fueller{i:02d}" for i in range(25)}
    ids = {}
    for rel in sorted(rels, key=lambda r: r.count("/")):
        n = rel.rsplit("/", 1)[-1]
        z = zerlege(n)
        parent = rel.rsplit("/", 1)[0] if "/" in rel else None
        ids[rel] = conn.execute(
            "INSERT INTO ablage_ordner (project_id, path, parent_id, rel_path, depth, name, label, praefix,"
            " codes, art, zuletzt_gesehen) VALUES (?,?,?,?,?,?,?,?,?,?,'x')",
            (pid, f"{pfad}/{rel}", ids.get(parent), rel, rel.count("/") + 1, n, z.label, z.praefix,
             json.dumps(z.codes), art_bestimmen(n, z.label))).lastrowid
    k = 0
    for rel, namen in ordner_dateien.items():
        for fn in namen:
            k += 1
            did = conn.execute(
                "INSERT INTO documents (project_id, hash, filename, extension, filesize, modified_at)"
                " VALUES (?,?,?,?,?,?)", (pid, f"h{name}{k}", fn, ".pdf", 10, "2026-09-01T00:00:00Z")).lastrowid
            conn.execute("INSERT INTO document_paths (document_id, path, is_primary) VALUES (?,?,1)",
                         (did, f"{pfad}/{rel}/{fn}"))
    conn.commit()
    return pid


def test_neu_aufbauen_schreibt_stats(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "_settings", {**settings._load(), "ablage": {}})
    for i in range(2):
        _projekt_mit_dateien(tmp_db, f"{211 + i} P", {
            "51_Ausfuehrung/51a_Planstände": [f"GR_EG_{j}.pdf" for j in range(6)],
            "b_Vertraege": [f"Werkvertrag_{j}.pdf" for j in range(4)],
            "b_Vertraege/z_Archiv": ["Werkvertrag_alt.pdf"],
        })
    vorlage.neu_berechnen(tmp_db)
    res = index.neu_aufbauen(tmp_db, JETZT)
    assert res["lern_projekte"] == 2 and res["zeilen"] > 0
    ebenen = {r[0] for r in tmp_db.execute("SELECT DISTINCT ebene FROM ablage_stats")}
    assert ebenen == {"ordner", "slot", "rolle", "global"}
    slot = tmp_db.execute("SELECT id FROM ablage_slot WHERE label_pfad='ausfuehrung/planstaende'").fetchone()[0]
    r = {m: g for m, g in tmp_db.execute(
        "SELECT merkmal, gewicht FROM ablage_stats WHERE ebene='slot' AND key_id=?", (slot,))}
    assert r["plantyp:GR"] > 0 and r["_n"] > 0
    glob = {m: g for m, g in tmp_db.execute("SELECT merkmal, gewicht FROM ablage_stats WHERE ebene='global'")}
    assert glob["_n"] > 20      # 2 × (6 + 4 + 0.5)

    # idempotent: zweiter Lauf ergibt dieselbe Tabelle
    vorher = sorted(tuple(r) for r in tmp_db.execute("SELECT * FROM ablage_stats"))
    index.neu_aufbauen(tmp_db, JETZT)
    assert sorted(tuple(r) for r in tmp_db.execute("SELECT * FROM ablage_stats")) == vorher


def test_neu_aufbauen_ohne_lern_projekte_leer(tmp_db):
    res = index.neu_aufbauen(tmp_db)
    assert res["zeilen"] == 1 or res["zeilen"] == 0       # nur die leere globale _n-Zeile oder nichts
    assert tmp_db.execute("SELECT COUNT(*) FROM ablage_stats WHERE ebene <> 'global'").fetchone()[0] == 0


def test_migration_032(tmp_db):
    assert tmp_db.execute("SELECT 1 FROM _migrations WHERE id='034_ablage_stats'").fetchone()
