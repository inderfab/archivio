"""Lernen von Norm-Ordnern statt Konfigurieren (siehe scanner/norms.py-Docstring
für den Portabilitäts-Hintergrund).

Nach jedem Scan aufgerufen: gruppiert klassifizierte Dokumente nach Elternordner
und schlägt Ordner vor, in denen ein hoher Anteil Normen ist. Vorschläge landen als
status='proposed' in norm_folders -- erst eine Bestätigung durch den Nutzer
(confirm_norm_folder) aktiviert die Laufzeit-Sperre in scanner/norms.py.

Bereits auf 'confirmed' oder 'rejected' gesetzte Ordner werden nie überschrieben."""
from __future__ import annotations

import logging
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def learn_norm_folders(conn: sqlite3.Connection, cfg: dict) -> int:
    """Aggregiert in Python statt per SQL GROUP BY auf einer generierten Spalte --
    documents.path liegt nicht vor (der Pfad steckt in document_paths), eine
    indizierte parent_dir-Spalte wäre für dieses gelegentliche Nachlauf-Update
    unnötiger Aufwand (Kommentar in der Spec selbst nennt das als Alternative).

    Gibt die Anzahl neu vorgeschlagener Ordner zurück."""
    fl_cfg = cfg.get("folder_learning", {})
    if not fl_cfg.get("enabled", True):
        return 0
    min_docs  = fl_cfg.get("min_norm_docs", 3)
    min_ratio = fl_cfg.get("min_ratio", 0.6)

    rows = conn.execute("""
        SELECT dp.path AS path, d.is_norm AS is_norm
        FROM document_paths dp
        JOIN documents d ON d.id = dp.document_id
        WHERE dp.is_primary = 1
    """).fetchall()

    counts: dict[str, list[int, int]] = defaultdict(lambda: [0, 0])  # [n_docs, n_norms]
    for r in rows:
        parent = os.path.dirname(r["path"])
        if not parent:
            continue
        counts[parent][0] += 1
        counts[parent][1] += r["is_norm"] or 0

    existing = {r["path"] for r in conn.execute("SELECT path FROM norm_folders").fetchall()}

    proposed = 0
    with conn:
        for path, (n_docs, n_norms) in counts.items():
            if n_norms < min_docs or n_docs == 0 or (n_norms / n_docs) < min_ratio:
                continue
            if path in existing:
                # Zaehler auf einem bereits proposed/confirmed/rejected Ordner
                # informativ nachfuehren, Status NIE anfassen.
                conn.execute(
                    "UPDATE norm_folders SET n_docs = ?, n_norms = ? WHERE path = ?",
                    (n_docs, n_norms, path),
                )
                continue
            conn.execute(
                "INSERT INTO norm_folders (path, status, n_docs, n_norms, detected_at) "
                "VALUES (?, 'proposed', ?, ?, ?)",
                (path, n_docs, n_norms, _now()),
            )
            proposed += 1
    if proposed:
        log.info("Norm-Ordner-Lernen: %d neue(r) Vorschlag/Vorschläge", proposed)
    return proposed


def confirm_norm_folder(conn: sqlite3.Connection, path: str) -> None:
    with conn:
        conn.execute(
            "UPDATE norm_folders SET status = 'confirmed', decided_at = ? WHERE path = ?",
            (_now(), path),
        )


def reject_norm_folder(conn: sqlite3.Connection, path: str) -> None:
    with conn:
        conn.execute(
            "UPDATE norm_folders SET status = 'rejected', decided_at = ? WHERE path = ?",
            (_now(), path),
        )


def add_manual_norm_folder(conn: sqlite3.Connection, path: str) -> None:
    """Von Hand als Norm-Ordner eingetragen -- anders als confirm_norm_folder() (das nur
    einen bereits per learn_norm_folders() ERKANNTEN Vorschlag bestätigt, ein UPDATE auf
    eine schon existierende Zeile) legt das hier bei Bedarf die Zeile neu an. Nötig für
    Ordner mit nur wenigen echten Normen, die den automatischen Anteils-Schwellwert
    (min_ratio/min_norm_docs in config/norms.yaml) nie erreichen und deshalb nie als
    Vorschlag auftauchen."""
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO norm_folders (path, status, n_docs, n_norms, detected_at, decided_at) "
            "VALUES (?, 'confirmed', 0, 0, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET status='confirmed', decided_at=excluded.decided_at",
            (path, now, now),
        )
