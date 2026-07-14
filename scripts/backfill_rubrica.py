#!/usr/bin/env python3
"""Einmaliger Backfill: holt für ALLE bereits bekannten Mails nochmal den vollen Body
(inkl. Signatur) per IMAP und schreibt ihn in rubrica.db.

Warum nötig: ein normaler inkrementeller Scan überspringt bekannte Mails schon beim
Header-Fetch (mail_scanner.py, mail_exists()-Check) — der volle Body wird für den
Altbestand nie erneut geholt. Dieses Skript lässt den normalen Scan-Hotpath unangetastet
und geht stattdessen unabhängig nochmal komplett über alle aktiven Postfächer.

Voraussetzung: rubrica.enabled: true in config.yaml (kein Sonderpfad am Flag vorbei).
Sicher wiederholt ausführbar — Dedup läuft gegen signatur_quelle.message_id in rubrica.db,
nicht gegen archivio.db.

Aufruf:
    .venv/bin/python scripts/backfill_rubrica.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
log = logging.getLogger("backfill_rubrica")


def main() -> None:
    from config import settings
    from db import connection
    from db.rubrica import get_rubrica_connection, save_signature_source
    from scanner.mail_scanner import (
        connect_imap, _fetch_uids, _fetch_header, _fetch_full, build_email_record,
    )

    if not settings.get("rubrica.enabled", False):
        log.error("rubrica.enabled ist nicht gesetzt (config.yaml) — Backfill übersprungen.")
        sys.exit(1)

    conn = connection.get_connection()
    mailboxes = conn.execute(
        """SELECT msc.mailbox_name, msc.project_id, p.name AS project_name
           FROM mail_scan_config msc
           LEFT JOIN projects p ON p.id = msc.project_id
           WHERE msc.active = 1
           ORDER BY msc.mailbox_name"""
    ).fetchall()
    conn.close()

    if not mailboxes:
        log.warning("Keine aktiven Postfächer gefunden.")
        return

    client = connect_imap()
    rconn  = get_rubrica_connection()

    total_new = total_skipped = total_errors = 0
    try:
        for row in mailboxes:
            mailbox      = row["mailbox_name"]
            project_id   = row["project_id"]
            project_name = row["project_name"] or ""

            if project_id is None:
                log.warning("Postfach '%s' hat kein Projekt — übersprungen", mailbox)
                continue

            try:
                uids = _fetch_uids(client, mailbox)
            except Exception as exc:
                log.warning("Postfach '%s' nicht lesbar: %s", mailbox, exc)
                continue

            log.info("Postfach '%s': %d Nachrichten", mailbox, len(uids))
            new = skipped = errors = 0

            for i, uid in enumerate(uids, start=1):
                try:
                    header_msg = _fetch_header(client, uid)
                    message_id = (header_msg.get("Message-ID") or "").strip()
                    if not message_id:
                        skipped += 1
                        continue

                    # Dedup gegen rubrica.db (NICHT gegen archivio.db — das ist ja gerade
                    # der Zweck: auch längst indexierte Mails erneut mit vollem Body holen).
                    exists = rconn.execute(
                        "SELECT 1 FROM signatur_quelle WHERE message_id=?", (message_id,)
                    ).fetchone()
                    if exists:
                        skipped += 1
                        continue

                    full_msg = _fetch_full(client, uid)
                    record   = build_email_record(full_msg, mailbox)
                    if save_signature_source(record, project_name, mailbox):
                        new += 1
                    else:
                        skipped += 1
                except Exception as exc:
                    errors += 1
                    log.warning("UID %s in '%s' übersprungen: %s", uid, mailbox, exc)

                if i % 100 == 0:
                    log.info("  '%s': %d/%d verarbeitet", mailbox, i, len(uids))

            log.info("'%s' fertig: %d neu, %d übersprungen, %d Fehler",
                      mailbox, new, skipped, errors)
            total_new += new; total_skipped += skipped; total_errors += errors

        log.info("Backfill abgeschlossen: %d neu, %d übersprungen, %d Fehler (gesamt)",
                  total_new, total_skipped, total_errors)
    finally:
        rconn.close()
        try:
            client.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
