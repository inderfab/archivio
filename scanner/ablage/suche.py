"""Ordnersuche im Projekt (Nachtrag 1 §3.1): Tippen filtert die Ordner des Projekts, auch Teilwörter.

Läuft aus `ablage_ordner` in der Datenbank, nie übers NAS und ohne Netzwerkzugriff. Die normalisierten Namen werden je
Projekt gemerkt (gültig, solange sich die Ordner nicht ändern), damit jede Eingabe nur noch vergleicht.
"""
from __future__ import annotations

import threading

from scanner.ablage.normalisieren import label_text

_cache: dict[int, tuple] = {}
_lock = threading.Lock()
LIMIT = 30


def _zeilen(conn, projekt_id: int) -> list[tuple]:
    stempel = tuple(conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(zuletzt_gesehen), '') FROM ablage_ordner WHERE project_id = ?",
        (projekt_id,)).fetchone())
    with _lock:
        e = _cache.get(projekt_id)
        if e and e[0] == stempel:
            return e[1]
    rows = []
    for r in conn.execute(
        "SELECT path, rel_path, name, label, depth FROM ablage_ordner "
        "WHERE project_id = ? AND art = 'normal' AND ausgeschlossen = 0", (projekt_id,)):
        # Name UND Label: „51a_Planstände" wird über „planst" und über „51a" gefunden
        rows.append((r["path"], r["rel_path"], r["name"], label_text(r["name"]) + " " + r["label"], r["depth"]))
    with _lock:
        _cache[projekt_id] = (stempel, rows)
    return rows


def suche_ordner(conn, projekt_id: int, anfrage: str, limit: int = LIMIT) -> list[dict]:
    """Ordner des Projekts, in denen jedes eingetippte Wort (auch als Wortteil) vorkommt. Treffer, bei denen alle Wörter
    am Wortanfang stehen, kommen zuerst, dann die flacheren Pfade."""
    woerter = label_text(anfrage or "").split()
    if not woerter:
        return []
    treffer = []
    for pfad, rel, name, norm, tiefe in _zeilen(conn, projekt_id):
        if all(w in norm for w in woerter):
            wortanfang = all(any(t.startswith(w) for t in norm.split()) for w in woerter)
            treffer.append((not wortanfang, tiefe, rel.lower(), pfad, rel, name))
    treffer.sort()
    return [{"pfad": p, "rel": rel, "name": name} for _, _, _, p, rel, name in treffer[:limit]]


def leeren() -> None:
    with _lock:
        _cache.clear()
