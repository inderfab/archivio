#!/usr/bin/env python3
"""Einmaliger Backfill: klassifiziert bereits gescannte Dokumente nachträglich als
Norm (SIA/VSS/EN/DIN/ISO) oder nicht.

Warum nötig: ein normaler inkrementeller Scan überspringt Dateien mit unverändertem
Pfad/Grösse/Änderungsdatum komplett (scanner/walker.py::_process_file, "Schnellpfad") --
_classify_norm() wird dabei nie aufgerufen. Für Bestände, die schon VOR der Norm-
Erkennung gescannt wurden, bleibt is_norm dadurch dauerhaft 0, auch nach einem erneuten
Scan desselben Ordners (Dateien auf dem NAS haben sich ja nicht geändert). Dieses
Skript klassifiziert den kompletten Bestand anhand des bereits gespeicherten Volltexts
(document_content) nach -- kein erneutes Lesen/Extrahieren der Originaldateien nötig.

Manuell gesetzte Overrides (norm_manual=1, siehe /norms) werden NIE angetastet.
Sicher wiederholt ausführbar -- klassifiziert jedes Mal den kompletten Bestand neu
(z.B. nützlich nach einer Verbesserung von config/norms.yaml).

Aufruf:
    .venv/bin/python scripts/backfill_norms.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
log = logging.getLogger("backfill_norms")


def main() -> None:
    from db import connection
    from scanner.norms import get_classifier

    conn = connection.get_connection()
    try:
        classifier = get_classifier(conn)
        if not classifier.enabled:
            log.error("Norm-Erkennung ist deaktiviert (config/norms.yaml) — Backfill übersprungen.")
            return

        rows = conn.execute("""
            SELECT d.id AS id, d.is_norm AS is_norm, dp.path AS path,
                   COALESCE(c.content, '') AS content
            FROM documents d
            JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
            LEFT JOIN document_content c ON c.document_id = d.id
            WHERE d.norm_manual = 0
        """).fetchall()

        total = len(rows)
        changed = newly_norm = 0
        for i, row in enumerate(rows, start=1):
            verdict = classifier.classify(row["path"], row["content"])
            new_is_norm = 1 if verdict.is_norm else 0
            if new_is_norm != row["is_norm"]:
                changed += 1
                if new_is_norm:
                    newly_norm += 1
                with conn:
                    conn.execute(
                        "UPDATE documents SET is_norm = ?, norm_reason = ? WHERE id = ?",
                        (new_is_norm, verdict.reason, row["id"]),
                    )
            if i % 500 == 0:
                log.info("  %d/%d verarbeitet…", i, total)

        log.info("Backfill abgeschlossen: %d Dokumente geprüft, %d Änderungen (%d neu als Norm erkannt).",
                  total, changed, newly_norm)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
