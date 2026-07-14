"""Brücke zur Rubrica-App (separates Adressbuch/CardDAV, liest diese DB).

Schreib-Vertrag (siehe PROJEKT_STATUS.md):
- Archivio schreibt AUSSCHLIESSLICH per INSERT OR IGNORE, nie UPDATE/DELETE, und setzt dabei
  immer status='pending'.
- Rubrica schreibt AUSSCHLIESSLICH status + status_updated_at zurück (UPDATE), fasst keine
  andere Spalte an.
Dadurch kann Rubrica einfach nach status='pending' pollen, statt bei jedem Lauf den ganzen
Bestand zu prüfen, und einmal abgelehnte Zeilen (status='rejected') werden nie erneut
vorgeschlagen.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from config import settings
from db import connection

_RUBRICA_DB_PATH: Path | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signatur_quelle (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id        TEXT UNIQUE NOT NULL,
    absender          TEXT,
    absender_email    TEXT,
    empfaenger        TEXT,
    cc                TEXT,
    postfach          TEXT,
    projekt           TEXT,
    betreff           TEXT,
    text              TEXT,
    datum             TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',
    status_updated_at TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_signatur_quelle_status ON signatur_quelle(status);
CREATE INDEX IF NOT EXISTS idx_signatur_quelle_absender_email ON signatur_quelle(absender_email);
"""


def _resolve_rubrica_path() -> Path:
    global _RUBRICA_DB_PATH
    if _RUBRICA_DB_PATH is None:
        raw = settings.get("rubrica.db_path", "") or ""
        if raw:
            path = Path(raw)
        else:
            # Default: gleiches Verzeichnis wie archivio.db
            path = connection._resolve_path().parent / "rubrica.db"
        _RUBRICA_DB_PATH = path
    return _RUBRICA_DB_PATH


def get_rubrica_connection() -> sqlite3.Connection:
    path = _resolve_rubrica_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # timeout=30: Archivio (INSERT) und Rubrica (UPDATE status) schreiben beide in dieselbe
    # Datei — WAL + Busy-Timeout wie bei archivio.db, damit sich beide nie blockieren.
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA)
    return conn


def save_signature_source(record: dict, project_name: str, mailbox_name: str) -> bool:
    """Spiegelt eine Mail (voller Text INKL. Signatur) für Rubrica. No-op wenn
    rubrica.enabled nicht gesetzt ist — einzige Stelle, die das Flag prüft.
    True = neu geschrieben, False = deaktiviert oder bereits vorhanden (message_id-Dedup)."""
    if not settings.get("rubrica.enabled", False):
        return False

    message_id = record.get("message_id")
    if not message_id:
        return False

    conn = get_rubrica_connection()
    try:
        with conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO signatur_quelle
                   (message_id, absender, absender_email, empfaenger, cc,
                    postfach, projekt, betreff, text, datum)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message_id,
                    record.get("sender", ""),
                    record.get("sender_email", ""),
                    record.get("recipients", ""),
                    record.get("cc", ""),
                    mailbox_name,
                    project_name,
                    record.get("subject", ""),
                    record.get("raw_text", ""),
                    record.get("mail_date", ""),
                ),
            )
            return cursor.rowcount > 0
    finally:
        conn.close()
