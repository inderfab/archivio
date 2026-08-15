#!/usr/bin/env python3
"""Einmalige Bereinigung: findet Dokumente mit mehr als einem is_primary=1-Pfad in
document_paths und demotet alle bis auf einen.

Warum nötig: upsert_path() (db/queries.py) setzte früher jeden neu gescannten Pfad
unbedingt als "primär", ohne einen bereits vorhandenen primären Pfad desselben
Dokuments zu demotieren. Bei Dokumenten mit mehreren physischen Kopien (identischer
Hash, z.B. "..._1-500.pdf" und "..._1-500-1.pdf") konnten so zwei gleichzeitig primäre
Pfade entstehen — der LEFT JOIN document_paths ... is_primary=1 in der Suche lieferte
dieselbe Dokument-ID dadurch mehrfach mit unterschiedlichem Pfad. upsert_path() ist
seit diesem Fix insofern korrigiert, dass sowas ab jetzt beim Schreiben nicht mehr
entsteht — dieses Skript räumt nur bereits vorhandene (vor dem Fix gescannte) Fälle auf.
Die Suche selbst dedupliziert defensiv (siehe web/main.py), ist also auch OHNE dieses
Skript schon korrekt — die Bereinigung macht die Daten selbst wieder konsistent.

Sicher wiederholt ausführbar (idempotent): nach einem Lauf gibt es keine Dokumente
mit mehreren primären Pfaden mehr, ein zweiter Lauf findet nichts zu tun.

Aufruf:
    .venv/bin/python scripts/fix_duplicate_primary_paths.py           # zeigt nur an
    .venv/bin/python scripts/fix_duplicate_primary_paths.py --apply   # schreibt
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
log = logging.getLogger("fix_duplicate_primary_paths")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true",
                         help="Änderungen tatsächlich schreiben (sonst nur anzeigen)")
    args = parser.parse_args()

    from db import connection

    conn = connection.get_connection()

    affected = conn.execute("""
        SELECT document_id, COUNT(*) AS n
        FROM document_paths
        WHERE is_primary = 1
        GROUP BY document_id
        HAVING COUNT(*) > 1
    """).fetchall()

    if not affected:
        log.info("Keine Dokumente mit mehreren primären Pfaden gefunden — nichts zu tun.")
        conn.close()
        return

    log.info("%d Dokument(e) mit mehreren primären Pfaden gefunden.", len(affected))

    demoted = 0
    for row in affected:
        doc_id = row["document_id"]
        paths = conn.execute(
            "SELECT id, path FROM document_paths WHERE document_id=? AND is_primary=1 "
            "ORDER BY id",
            (doc_id,),
        ).fetchall()
        # Ältesten (kleinste id) Pfad als primär behalten, alle anderen demotieren.
        keep, *drop = paths
        log.info("Dokument %d: behalte primär %s, demote %s",
                  doc_id, keep["path"], [p["path"] for p in drop])
        if args.apply:
            conn.executemany(
                "UPDATE document_paths SET is_primary = 0 WHERE id = ?",
                [(p["id"],) for p in drop],
            )
        demoted += len(drop)

    if args.apply:
        conn.commit()
        log.info("Fertig: %d Pfad(e) demotet, %d Dokument(e) bereinigt.", demoted, len(affected))
    else:
        log.info("Trockenlauf — %d Pfad(e) würden demotet (mit --apply tatsächlich schreiben).",
                  demoted)

    conn.close()


if __name__ == "__main__":
    main()
