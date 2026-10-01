"""Büroweite Vorlage („Slots"): aus einem Musterordner importiert (Weg A) oder aus den
Projekten hergeleitet (Weg B), danach jedem Ordner zugeordnet.

Ein Slot ist ein Label-Pfad (`ausfuehrung/planstaende`), nie ein Präfix oder BKP-Code.
Gibt es Weg A, gewinnt er für die Struktur; die Projekte liefern dann nur die Abdeckung
und später die Statistik. Nichts hier ist büro-spezifisch: Strut-Eigenheiten stehen in
der Konfiguration (`ablage.*`), nicht im Code.
"""
from __future__ import annotations

import logging
import os
import re
from collections import Counter
from functools import lru_cache

from config import settings
from scanner.ablage.normalisieren import ist_trenner, label_pfad, pfad_schluessel

log = logging.getLogger(__name__)

MIN_ORDNER = 20          # Projekte mit weniger Ordnern lernen nicht mit
ABDECKUNG_MIN = 0.5      # Slot gilt ab diesem Anteil Lern-Projekte
AEHNLICHKEIT_MIN = 0.3   # darunter weicht ein Projekt zu stark von der Vorlage ab
_NUMMER_RE_STANDARD = r"^\s*(\d+)(?!\d)"


# ── Konfiguration ──────────────────────────────────────────────────────────────

def musterordner_pfad() -> str:
    roh = settings.get("ablage.musterordner") or ""
    return pfad_schluessel(roh) if roh else ""


def ist_musterprojekt(pfad: str) -> bool:
    """Der Musterordner (bei Strut `000 Musterordner Objekt`) ist kein Projekt für Vorschläge."""
    m = musterordner_pfad()
    return bool(m) and pfad_schluessel(pfad) == m


def projekt_nummer(name: str, pfad: str = "") -> int | None:
    rx = settings.get("ablage.projektnummer_regex") or _NUMMER_RE_STANDARD
    try:
        regex = re.compile(rx)
    except re.error:
        regex = re.compile(_NUMMER_RE_STANDARD)
    for text in (name, os.path.basename(pfad.rstrip("/"))):
        m = regex.search(text or "")
        if m:
            try:
                return int(m.group(1))
            except (ValueError, IndexError):
                return None
    return None


# ── Lern-Projekte ──────────────────────────────────────────────────────────────

def lern_projekte(conn) -> list[int]:
    """Alle Projekte ausser Musterordner, Postfächern, Projekten unter der konfigurierten
    Nummerngrenze (`ablage.lernen_ab_projektnummer`; Projekte ohne Nummer zählen dann nicht)
    und Projekten mit weniger als MIN_ORDNER Ordnern."""
    ab = settings.get("ablage.lernen_ab_projektnummer")
    try:
        ab = int(ab) if ab not in (None, "") else None
    except (TypeError, ValueError):
        ab = None
    rows = conn.execute(
        "SELECT p.id, p.name, p.path, COUNT(o.id) AS n FROM projects p "
        "JOIN ablage_ordner o ON o.project_id = p.id AND o.ausgeschlossen = 0 "
        "GROUP BY p.id"
    ).fetchall()
    res = []
    for r in rows:
        if r["path"].startswith("mailbox:") or ist_musterprojekt(r["path"]):
            continue
        if r["n"] < MIN_ORDNER:
            continue
        if ab is not None:
            nr = projekt_nummer(r["name"], r["path"])
            if nr is None or nr < ab:
                continue
        res.append(r["id"])
    return res


# ── Label-Pfade ────────────────────────────────────────────────────────────────

@lru_cache(maxsize=300_000)
def _lp(rel_path: str) -> str:
    return label_pfad(rel_path)


def _projekt_labelpfade(conn, projekt_ids) -> tuple[dict[int, set[str]], dict[str, Counter]]:
    """(Projekt → Menge Label-Pfade, Label-Pfad → Zähler der Originalnamen des Blattordners)."""
    ids = set(projekt_ids)
    pro_projekt: dict[int, set[str]] = {i: set() for i in ids}
    namen: dict[str, Counter] = {}
    for r in conn.execute(
        "SELECT project_id, rel_path, name FROM ablage_ordner WHERE art IN ('normal','archiv') AND ausgeschlossen = 0"
    ):
        if r["project_id"] not in ids:
            continue
        lp = _lp(r["rel_path"])
        if not lp:
            continue
        pro_projekt[r["project_id"]].add(lp)
        namen.setdefault(lp, Counter())[r["name"]] += 1
    return pro_projekt, namen


def _abdeckung(pro_projekt: dict[int, set[str]]) -> dict[str, float]:
    n = len(pro_projekt)
    if not n:
        return {}
    z: Counter = Counter()
    for s in pro_projekt.values():
        z.update(s)
    return {lp: c / n for lp, c in z.items()}


def herleiten(pro_projekt: dict[int, set[str]], abdeckung_min: float = ABDECKUNG_MIN,
              aehnlichkeit_min: float = AEHNLICHKEIT_MIN,
              iterationen: int = 2) -> tuple[dict[str, float], list[int]]:
    """Weg B. Gibt (Slot → Abdeckung, verwendete Projekte) zurück.

    Projekte, deren Jaccard-Ähnlichkeit zur hergeleiteten Vorlage unter `aehnlichkeit_min`
    liegt (alte Struktur, fremdes Schema), zählen nicht mit; danach wird einmal neu berechnet.
    """
    behalten = dict(pro_projekt)
    vorlage: set[str] = set()
    for _ in range(max(1, iterationen)):
        abd = _abdeckung(behalten)
        vorlage = {lp for lp, a in abd.items() if a >= abdeckung_min}
        if not vorlage:
            break
        neu = {p: s for p, s in behalten.items()
               if len(s & vorlage) / max(1, len(s | vorlage)) >= aehnlichkeit_min}
        if not neu or len(neu) == len(behalten):
            break
        behalten = neu
    abd = _abdeckung(behalten)
    return {lp: abd[lp] for lp in vorlage}, sorted(behalten)


# ── Musterordner (Weg A) ───────────────────────────────────────────────────────

def _lies_baum(pfad: str) -> list[str]:
    """Relative Ordnerpfade eines Baums. Einmaliger os.walk — hier erlaubt (nicht im Vorschlagspfad)."""
    res = []
    for dirpath, dirnames, _ in os.walk(pfad):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for d in dirnames:
            rel = os.path.relpath(os.path.join(dirpath, d), pfad)
            res.append(pfad_schluessel(rel))
    return res


def importiere_musterordner(conn, pfad: str) -> dict:
    """Weg A: Baum lesen und als Slots (`aus_vorlage=1`) speichern. Ersetzt eine frühere Vorlage."""
    if not pfad or not os.path.isdir(pfad):
        raise FileNotFoundError(f"Musterordner nicht gefunden: {pfad}")
    ordner = _lies_baum(pfad)
    slots: dict[str, Counter] = {}
    for rel in ordner:
        if any(ist_trenner(t) for t in rel.split("/")):
            continue
        lp = _lp(rel)
        if lp:
            slots.setdefault(lp, Counter())[rel.rsplit("/", 1)[-1]] += 1
    with conn:
        # A gewinnt für die Struktur: auch hergeleitete Slots gelten nicht mehr
        conn.execute("UPDATE ablage_ordner SET slot_id = NULL WHERE slot_id IS NOT NULL")
        conn.execute("DELETE FROM ablage_slot")
        for lp, namen in slots.items():
            conn.execute(
                "INSERT INTO ablage_slot (label_pfad, rolle, anzeige, abdeckung, aus_vorlage) "
                "VALUES (?,?,?,0,1)",
                (lp, lp.rsplit("/", 1)[-1], namen.most_common(1)[0][0]),
            )
    return {"ordner": len(ordner), "slots": len(slots)}


def vorlage_entfernen(conn) -> None:
    """Musterordner nicht mehr konfiguriert: Vorlage-Slots weg, Herleitung übernimmt."""
    with conn:
        conn.execute("UPDATE ablage_ordner SET slot_id = NULL WHERE slot_id IN "
                     "(SELECT id FROM ablage_slot WHERE aus_vorlage = 1)")
        conn.execute("DELETE FROM ablage_slot WHERE aus_vorlage = 1")


# ── Zuordnung und Neuberechnung ────────────────────────────────────────────────

def zuordnen(conn, project_id: int | None = None) -> int:
    """Setzt `slot_id` jedes Ordners anhand seines Label-Pfads (optional nur eines Projekts, so läuft
    es direkt nach dem Scan). Gibt die Zahl der Änderungen zurück."""
    slot_ids = {r["label_pfad"]: r["id"] for r in conn.execute("SELECT id, label_pfad FROM ablage_slot")}
    aenderungen = []
    sql = "SELECT id, rel_path, art, slot_id FROM ablage_ordner"
    for r in (conn.execute(sql + " WHERE project_id = ?", (project_id,)) if project_id is not None
              else conn.execute(sql)):
        neu = slot_ids.get(_lp(r["rel_path"])) if r["art"] in ("normal", "archiv") else None
        if neu != r["slot_id"]:
            aenderungen.append((neu, r["id"]))
    if aenderungen:
        with conn:
            conn.executemany("UPDATE ablage_ordner SET slot_id = ? WHERE id = ?", aenderungen)
    return len(aenderungen)


def neu_berechnen(conn) -> dict:
    """Slots aktualisieren (Abdeckung bzw. Herleitung) und alle Ordner zuordnen."""
    lern = lern_projekte(conn)
    pro_projekt, namen = _projekt_labelpfade(conn, lern)
    vorlage_slots = {r["label_pfad"] for r in conn.execute(
        "SELECT label_pfad FROM ablage_slot WHERE aus_vorlage = 1")}

    if vorlage_slots and musterordner_pfad():
        # Weg A: Struktur steht fest. Projekte, die kaum in die Vorlage passen, zählen nicht.
        behalten = {p: s for p, s in pro_projekt.items()
                    if s and len(s & vorlage_slots) / len(s) >= AEHNLICHKEIT_MIN} or pro_projekt
        abd = _abdeckung(behalten)
        with conn:
            conn.execute("UPDATE ablage_slot SET abdeckung = 0 WHERE aus_vorlage = 1")
            conn.executemany("UPDATE ablage_slot SET abdeckung = ? WHERE label_pfad = ?",
                             [(a, lp) for lp, a in abd.items() if lp in vorlage_slots])
        genutzt = sorted(behalten)
        weg = "A"
    else:
        slots, genutzt = herleiten(pro_projekt)
        with conn:
            # Upsert statt Löschen+Neuanlegen: Slot-IDs bleiben stabil, sonst würde jede
            # Neuberechnung alle Zuordnungen verwerfen (und ablage_stats später ins Leere zeigen)
            for lp, a in slots.items():
                anzeige = namen.get(lp, Counter({lp.rsplit("/", 1)[-1]: 1})).most_common(1)[0][0]
                conn.execute(
                    "INSERT INTO ablage_slot (label_pfad, rolle, anzeige, abdeckung, aus_vorlage) "
                    "VALUES (?,?,?,?,0) ON CONFLICT(label_pfad) DO UPDATE SET "
                    "anzeige=excluded.anzeige, abdeckung=excluded.abdeckung", (lp, lp.rsplit("/", 1)[-1], anzeige, a))
            veraltet = [r["id"] for r in conn.execute("SELECT id, label_pfad FROM ablage_slot")
                        if r["label_pfad"] not in slots]
            conn.executemany("UPDATE ablage_ordner SET slot_id = NULL WHERE slot_id = ?", [(i,) for i in veraltet])
            conn.executemany("DELETE FROM ablage_slot WHERE id = ?", [(i,) for i in veraltet])
        weg = "B"
    geaendert = zuordnen(conn)
    n_slots = conn.execute("SELECT COUNT(*) FROM ablage_slot").fetchone()[0]
    log.info("Ablage-Struktur (Weg %s): %d Slots, %d Lern-Projekte, %d Zuordnungen geändert",
             weg, n_slots, len(genutzt), geaendert)
    return {"weg": weg, "slots": n_slots, "lern_projekte": len(lern), "genutzt": len(genutzt),
            "zugeordnet_geaendert": geaendert}


def struktur_uebersicht(conn) -> dict:
    """Für „Erkannte Struktur" in den Einstellungen: Slot-Baum mit Abdeckung und Dateien."""
    rows = conn.execute(
        "SELECT s.label_pfad, s.anzeige, s.abdeckung, s.aus_vorlage, "
        "COALESCE(SUM(o.datei_anzahl), 0) AS dateien, COUNT(o.id) AS ordner "
        "FROM ablage_slot s LEFT JOIN ablage_ordner o ON o.slot_id = s.id "
        "GROUP BY s.id"
    ).fetchall()
    rows = sorted(rows, key=lambda r: r["label_pfad"].split("/"))   # Baumreihenfolge, nicht Zeichenfolge
    slots = [{
        "label_pfad": r["label_pfad"], "tiefe": r["label_pfad"].count("/"),
        "anzeige": r["anzeige"], "abdeckung_pct": round(r["abdeckung"] * 100),
        "dateien": r["dateien"], "ordner": r["ordner"], "aus_vorlage": bool(r["aus_vorlage"]),
    } for r in rows]
    return {
        "slots": slots,
        "aus_vorlage": any(s["aus_vorlage"] for s in slots),
        "lern_projekte": len(lern_projekte(conn)),
        "projekte_mit_ordnern": conn.execute(
            "SELECT COUNT(DISTINCT project_id) FROM ablage_ordner").fetchone()[0],
    }
