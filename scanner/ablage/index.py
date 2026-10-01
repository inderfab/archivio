"""Statistik für den Ablage-Vorschlag: Merkmalszählungen je Ordner, Slot, Rolle und global.

Aufgebaut wird aus Dateiname, Endung und Mail-Metadaten (kein Inhalt, das hält den Neuaufbau
schnell). Jede Datei zählt mit `w = 0.5 ** (alter_tage / 730)`. Dateien in Archiv-Ordnern
zählen mit halbem Gewicht für ihren Elternordner: was im Archiv liegt, lag einmal dort.
`statistik_berechnen` ist rein (kein DB-Zugriff) und wird vom Messskript wiederverwendet.
"""
from __future__ import annotations

import logging
import os
import zlib
from collections import defaultdict
from datetime import datetime, timezone

from scanner.ablage import merkmale as mk
from scanner.ablage import vorlage
from scanner.ablage.normalisieren import pfad_schluessel

log = logging.getLogger(__name__)

HALBWERTSZEIT_TAGE = 730
MIN_GEWICHT = 2.0          # Merkmale mit kleinerem Gesamtgewicht werden verworfen
ORDNER_MIN_GEWICHT = 0.0   # je Ordner kein weiteres Limit: kleine, neue Ordner leben von wenig Evidenz
ARCHIV_FAKTOR = 0.5
N = "_n"


def rolle_key(rolle: str) -> int:
    """Stabile Zahl für eine Rolle (Label des Blattordners, phasenübergreifend)."""
    return zlib.crc32(rolle.encode("utf-8"))


def alters_gewicht(modified_at, jetzt: datetime) -> float:
    d = mk._parse_mtime(modified_at)
    if d is None:
        return 0.5
    tage = max(0.0, (jetzt - d).total_seconds() / 86400)
    return 0.5 ** (tage / HALBWERTSZEIT_TAGE)


def statistik_berechnen(dateien, ordner: dict, wb: mk.Woerterbuch | None = None,
                        jetzt: datetime | None = None, nur_vor: str | None = None,
                        min_gewicht: float = MIN_GEWICHT) -> dict:
    """Aggregiert Merkmale. `dateien`: Zeilen mit id, filename, modified_at, ordner_id, optional mail.
    `ordner`: id → {slot_id, rolle, art, parent_id}. `nur_vor`: ISO-Zeit, jüngere Dateien zählen nicht.

    Rückgabe: {(ebene, key_id): {merkmal: gewicht}} inkl. Gesamtgewicht unter `_n`.
    """
    wb = wb or mk.woerterbuch()
    jetzt = jetzt or datetime.now(timezone.utc)
    je_ordner: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    gesamt: dict[str, float] = defaultdict(float)

    for f in dateien:
        if nur_vor and (f["modified_at"] or "") >= nur_vor:
            continue
        o = ordner.get(f["ordner_id"])
        if o is None:
            continue
        w = alters_gewicht(f["modified_at"], jetzt)
        if o["art"] == "archiv":
            ziel = o.get("parent_id")
            if ziel is None or ziel not in ordner:
                continue
            w *= ARCHIV_FAKTOR
        elif o["art"] == "normal":
            ziel = f["ordner_id"]
        else:
            continue
        feat = mk.merkmale(f["filename"], f.get("filesize"), f["modified_at"], None, f.get("mail"), wb)
        z = je_ordner[ziel]
        z[N] += w
        gesamt[N] += w
        for k in feat:
            z[k] += w
            gesamt[k] += w

    behalten = {k for k, g in gesamt.items() if g >= min_gewicht or k == N}
    res: dict = {}
    slot: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    rolle: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for oid, z in je_ordner.items():
        info = ordner[oid]
        rk = rolle_key(info["rolle"]) if info.get("rolle") else None
        for k, g in z.items():
            if k not in behalten:
                continue
            if info.get("slot_id") is not None:
                slot[info["slot_id"]][k] += g
            if rk is not None:
                rolle[rk][k] += g
    for oid, z in je_ordner.items():
        res[("ordner", oid)] = {k: g for k, g in z.items()
                                if k == N or (k in behalten and g >= ORDNER_MIN_GEWICHT)}
    for sid, z in slot.items():
        res[("slot", sid)] = dict(z)
    for rk, z in rolle.items():
        res[("rolle", rk)] = dict(z)
    res[("global", 0)] = {k: g for k, g in gesamt.items() if k in behalten}
    return res


def _lade_dateien(conn, projekt_ids, ordner_nach_pfad: dict):
    for pid in projekt_ids:
        for r in conn.execute(
            "SELECT d.id, d.filename, d.filesize, d.modified_at, dp.path FROM document_paths dp "
            "JOIN documents d ON d.id = dp.document_id "
            "WHERE d.project_id = ? AND dp.is_primary = 1 AND d.source_type = 'filesystem'", (pid,)):
            oid = ordner_nach_pfad.get(pfad_schluessel(os.path.dirname(r["path"])))
            if oid is not None:
                yield {"id": r["id"], "filename": r["filename"], "filesize": r["filesize"],
                       "modified_at": r["modified_at"], "ordner_id": oid}


def lade_ordner(conn, projekt_ids) -> tuple[dict, dict]:
    """(id → Ordnerinfo, Pfad → id) der angegebenen Projekte."""
    ordner, nach_pfad = {}, {}
    ids = set(projekt_ids)
    for r in conn.execute("SELECT id, project_id, path, parent_id, label, art, slot_id FROM ablage_ordner"):
        if r["project_id"] in ids:
            ordner[r["id"]] = {"slot_id": r["slot_id"], "rolle": r["label"], "art": r["art"],
                               "parent_id": r["parent_id"]}
            nach_pfad[r["path"]] = r["id"]
    return ordner, nach_pfad


def bkp_woerter(conn) -> dict[str, str]:
    """Gelernte BKP-Bezeichnungen aus allen Ordnernamen (je Name und Projekt einmal gezählt)."""
    zeilen = []
    for r in conn.execute(
        "SELECT DISTINCT o.project_id, o.name, o.label, o.codes, p.name AS pname, p.path AS ppath "
        "FROM ablage_ordner o JOIN projects p ON p.id = o.project_id WHERE o.art = 'normal'"):
        zeilen.append((r["label"], r["codes"], vorlage.projekt_nummer(r["pname"], r["ppath"])))
    return mk.bkp_woerter_aus_ordnern(zeilen, mk.woerterbuch().ist_allgemein)


def neu_aufbauen(conn, jetzt: datetime | None = None) -> dict:
    """Statistik komplett neu berechnen und in `ablage_stats` schreiben."""
    lern = vorlage.lern_projekte(conn)
    ordner, nach_pfad = lade_ordner(conn, lern)
    wb = mk.woerterbuch().mit_bkp_woertern(bkp_woerter(conn))
    stat = statistik_berechnen(_lade_dateien(conn, lern, nach_pfad), ordner, wb, jetzt)
    zeilen = [(e, k, m, g) for (e, k), z in stat.items() for m, g in z.items()]
    with conn:
        conn.execute("DELETE FROM ablage_stats")
        conn.executemany("INSERT INTO ablage_stats (ebene, key_id, merkmal, gewicht) VALUES (?,?,?,?)", zeilen)
    n = stat.get(("global", 0), {}).get(N, 0.0)
    from scanner.ablage import kontext
    kontext.invalidieren()
    log.info("Ablage-Statistik: %d Zeilen, %d Lern-Projekte, Gesamtgewicht %.0f", len(zeilen), len(lern), n)
    return {"zeilen": len(zeilen), "lern_projekte": len(lern), "gesamtgewicht": n}
