"""Ordner-Erfassung beim Scan: der Walker sammelt im Speicher, geschrieben wird einmal am Ende.

Pro Ordner nicht in die DB zu schreiben ist Absicht (PROJEKT_STATUS §6: Autocommit,
`skip_conn`, „database is locked"). Der Walker ruft `OrdnerSammler.ordner_gesehen()` /
`.dateien_gesehen()`/`.mtime_gesehen()` auf, `schreibe_ordner()` läuft danach in EINER Transaktion zusammen mit
dem Aufräumen gelöschter Dateien.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from functools import lru_cache

from scanner.ablage.normalisieren import art_bestimmen, pfad_schluessel, zerlege

log = logging.getLogger(__name__)


@lru_cache(maxsize=100_000)
def _zerlegt(name: str) -> tuple[str, str, str]:
    """(Präfix, Label, Codes als JSON) — gleiche Namen kommen in jedem Projekt tausendfach vor."""
    z = zerlege(name)
    return z.praefix, z.label, json.dumps(z.codes)


class OrdnerSammler:
    """Hält Ordner und ihre direkten Dateien eines Projekt-Scans im Speicher."""

    def __init__(self, root: str, archiv_labels=None):
        self.root = pfad_schluessel(root)
        self.archiv_labels = archiv_labels
        self._ordner: dict[str, str] = {}          # Pfad → art
        self._dateien: dict[str, list] = {}        # Pfad → [Anzahl, jüngste mtime (ISO) | None]

    def ordner_gesehen(self, pfad: str, name: str, ausgeschlossen: bool = False) -> None:
        self._ordner[pfad] = art_bestimmen(name, _zerlegt(name)[1], self.archiv_labels, ausgeschlossen)

    def dateien_gesehen(self, ordner: str, anzahl: int) -> None:
        self._dateien.setdefault(ordner, [0, None])[0] += anzahl

    def mtime_gesehen(self, ordner: str, mtime_iso: str) -> None:
        """Jüngste Änderung eines Ordners. Nutzt das stat() des Skip-Checks, kostet nichts extra."""
        e = self._dateien.setdefault(ordner, [0, None])
        if e[1] is None or mtime_iso > e[1]:
            e[1] = mtime_iso

    def __len__(self) -> int:
        return len(self._ordner)

    def datensaetze(self) -> list[dict]:
        """Eltern vor Kindern (nach Tiefe sortiert), damit parent_id beim Schreiben bekannt ist."""
        res = []
        root = self.root
        for pfad, art in self._ordner.items():
            key = pfad_schluessel(pfad)
            rel = key[len(root):].lstrip("/") if key.startswith(root) else key
            name = os.path.basename(key)
            praefix, label, codes = _zerlegt(name)
            anzahl, mtime = self._dateien.get(pfad, (0, None))
            res.append({
                "path": key, "rel_path": rel, "depth": rel.count("/") + 1, "name": name,
                "label": label, "praefix": praefix, "codes": codes,
                "art": art, "datei_anzahl": anzahl, "letzte_aenderung": mtime,
                "parent": key.rsplit("/", 1)[0] if "/" in rel else None,
            })
        res.sort(key=lambda d: (d["depth"], d["path"]))
        return res


_FELDER = ("parent_id", "rel_path", "depth", "name", "label", "praefix", "codes", "art",
           "datei_anzahl", "letzte_aenderung")


def schreibe_ordner(conn, project_id: int, sammler: OrdnerSammler) -> int:
    """Schreibt die gesammelten Ordner in einer Transaktion; Ordner dieses Projekts, die der
    Scan nicht mehr sah, verschwinden (Kinder per ON DELETE CASCADE). Gibt die Anzahl zurück.

    Unveränderte Zeilen werden nicht neu geschrieben (ein Re-Scan ändert fast nichts); nur
    `zuletzt_gesehen` wird per Sammel-UPDATE nachgezogen. `slot_id` bleibt immer unangetastet
    (wird von der Vorlage-Zuordnung gesetzt).
    """
    marke = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    daten = sammler.datensaetze()
    with conn:
        vorhanden = {r["path"]: r for r in conn.execute(
            "SELECT id, path, " + ", ".join(_FELDER) + " FROM ablage_ordner WHERE project_id = ?",
            (project_id,))}
        ids: dict[str, int] = {}
        for d in daten:
            parent_id = ids.get(d["parent"]) if d["parent"] else None
            alt = vorhanden.get(d["path"])
            if alt is not None:
                if all(alt[f] == (parent_id if f == "parent_id" else d[f]) for f in _FELDER):
                    ids[d["path"]] = alt["id"]
                    continue
            row = conn.execute(
                """INSERT INTO ablage_ordner
                       (project_id, path, parent_id, rel_path, depth, name, label, praefix,
                        codes, art, datei_anzahl, letzte_aenderung, zuletzt_gesehen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET
                       project_id=excluded.project_id, parent_id=excluded.parent_id,
                       rel_path=excluded.rel_path, depth=excluded.depth, name=excluded.name,
                       label=excluded.label, praefix=excluded.praefix, codes=excluded.codes,
                       art=excluded.art, datei_anzahl=excluded.datei_anzahl,
                       letzte_aenderung=excluded.letzte_aenderung,
                       zuletzt_gesehen=excluded.zuletzt_gesehen
                   RETURNING id""",
                (project_id, d["path"], parent_id, d["rel_path"], d["depth"], d["name"],
                 d["label"], d["praefix"], d["codes"], d["art"], d["datei_anzahl"],
                 d["letzte_aenderung"], marke),
            ).fetchone()
            ids[d["path"]] = row[0]
        # Gesehen markieren (ein Sammel-UPDATE) und entfernen, was der Scan nicht mehr fand
        conn.execute("UPDATE ablage_ordner SET zuletzt_gesehen = ? WHERE project_id = ?",
                     (marke, project_id))
        gesehen = set(ids.values())
        veraltet = [(r["id"],) for r in vorhanden.values() if r["id"] not in gesehen]
        conn.executemany("DELETE FROM ablage_ordner WHERE id = ?", veraltet)
    return len(daten)
