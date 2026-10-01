"""Inkrementelle Statistik nach jeder Ablage (Etappe 8, reduziert; Nachtrag 1 §5).

Die abgelegte Datei zählt sofort mit, nicht erst nach dem nächsten Neuaufbau: ihre Merkmale werden auf den vier Ebenen
(Ordner, Slot, Rolle, global) mit Gewicht 1 addiert, in `ablage_stats` UND im Speicher-Cache. Ein Rückgängig nimmt
genau das wieder weg. Der nächtliche Neuaufbau (`index.neu_aufbauen`) ersetzt später alles durch die altersgewichtete Fassung.
Regel-Vorschläge sind bewusst zurückgestellt, bis Daten aus dem Alltag da sind.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from scanner.ablage import merkmale as mk
from scanner.ablage.index import ARCHIV_FAKTOR, N, rolle_key
from scanner.ablage.normalisieren import art_bestimmen, pfad_schluessel, zerlege

log = logging.getLogger(__name__)


def _jetzt() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ordner_registrieren(conn, projekt_id: int, ordner_pfad: str, projekt_pfad: str) -> int | None:
    """Einen neu angelegten Ordner (z. B. „Neuer Ordner 260918_Thema") in `ablage_ordner` eintragen. Der Elternordner muss
    schon erfasst sein (oder das Projekt selbst). Gibt die ID zurück, bei unbekanntem Elternordner None."""
    from config import settings
    p = pfad_schluessel(ordner_pfad)
    r = conn.execute("SELECT id FROM ablage_ordner WHERE path = ?", (p,)).fetchone()
    if r:
        return r["id"]
    basis = pfad_schluessel(projekt_pfad).rstrip("/")
    if not p.startswith(basis + "/"):
        return None
    rel = p[len(basis) + 1:]
    eltern_pfad = os.path.dirname(p)
    parent_id = None
    if eltern_pfad != basis:
        e = conn.execute("SELECT id FROM ablage_ordner WHERE path = ?", (eltern_pfad,)).fetchone()
        if not e:
            return None
        parent_id = e["id"]
    name = os.path.basename(p)
    z = zerlege(name)
    with conn:
        cur = conn.execute(
            "INSERT INTO ablage_ordner (project_id, path, parent_id, rel_path, depth, name, label, praefix, codes, art,"
            " ausgeschlossen, datei_anzahl, zuletzt_gesehen) VALUES (?,?,?,?,?,?,?,?,?,?,0,0,?)",
            (projekt_id, p, parent_id, rel, rel.count("/") + 1, name, z.label, z.praefix, json.dumps(z.codes),
             art_bestimmen(name, z.label, settings.get("ablage.archiv_labels")), _jetzt()))
    return cur.lastrowid


def _ziel(conn, ordner_id: int) -> tuple[int, float] | None:
    """Ordner, dem die Datei statistisch zugerechnet wird: der Ordner selbst, ein Archiv-Ordner zählt halb für den Eltern."""
    r = conn.execute("SELECT id, art, parent_id FROM ablage_ordner WHERE id = ?", (ordner_id,)).fetchone()
    if r is None:
        return None
    if r["art"] == "archiv":
        return (r["parent_id"], ARCHIV_FAKTOR) if r["parent_id"] else None
    return (r["id"], 1.0) if r["art"] == "normal" else None


def _addieren(conn, ordner_id: int, merkmale: list[str], gewicht: float) -> dict:
    """Merkmale + Gesamtgewicht auf allen vier Ebenen addieren (negatives Gewicht = zurücknehmen)."""
    o = conn.execute("SELECT id, slot_id, label FROM ablage_ordner WHERE id = ?", (ordner_id,)).fetchone()
    ebenen = [("ordner", o["id"]), ("global", 0), ("rolle", rolle_key(o["label"]))]
    if o["slot_id"] is not None:
        ebenen.append(("slot", o["slot_id"]))
    zeilen = [(e, k, m, gewicht) for e, k in ebenen for m in [*merkmale, N]]
    with conn:
        conn.executemany(
            "INSERT INTO ablage_stats (ebene, key_id, merkmal, gewicht) VALUES (?,?,?,?) "
            "ON CONFLICT(ebene, key_id, merkmal) DO UPDATE SET gewicht = MAX(0, gewicht + excluded.gewicht)", zeilen)
    return {"ordner_id": o["id"], "slot_id": o["slot_id"], "rolle": o["label"]}


def nach_ablage(conn, vorgang: dict, final_path: str) -> dict | None:
    """Nach erfolgreicher Ablage aufrufen. Gibt zurück, was addiert wurde (für ein späteres Rückgängig) oder None."""
    from scanner.ablage import kontext, suche
    e = vorgang.get("entscheid") or {}
    ordner_pfad = pfad_schluessel(os.path.dirname(final_path))
    r = conn.execute("SELECT id FROM ablage_ordner WHERE path = ?", (ordner_pfad,)).fetchone()
    neu = False
    if r is None:
        oid = ordner_registrieren(conn, e["projekt_id"], ordner_pfad, e["projekt_pfad"])
        if oid is None:
            return None
        from scanner.ablage import vorlage
        vorlage.zuordnen(conn, e["projekt_id"])                    # Slot des neuen Ordners
        neu = True
    else:
        oid = r["id"]
    ziel = _ziel(conn, oid)
    if ziel is None:
        return None
    ziel_id, gewicht = ziel
    merk = [m for m in (vorgang.get("merkmale") or {}) if not m.startswith("inh:")]
    info = _addieren(conn, ziel_id, merk, gewicht)
    with conn:
        conn.execute("UPDATE ablage_ordner SET datei_anzahl = datei_anzahl + 1, letzte_aenderung = ? WHERE id = ?",
                     (_jetzt(), oid))
    info.update(gewicht=gewicht, merkmale=merk, ablage_ordner_id=oid, neu=neu)
    if neu:
        kontext.invalidieren()                                      # Struktur hat sich geändert → neu laden
        suche.leeren()
    else:
        kontext.inkrementell(info, +1, conn)
    return info


def zurueck(conn, info: dict) -> None:
    """Macht `nach_ablage` rückgängig (Rückgängig innerhalb der 5 Minuten)."""
    from scanner.ablage import kontext
    if not info:
        return
    _addieren(conn, info["ordner_id"], info["merkmale"], -info["gewicht"])
    with conn:
        conn.execute("UPDATE ablage_ordner SET datei_anzahl = MAX(0, datei_anzahl - 1) WHERE id = ?",
                     (info["ablage_ordner_id"],))
    if info.get("neu"):
        kontext.invalidieren()
    else:
        kontext.inkrementell(info, -1, conn)
