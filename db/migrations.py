from __future__ import annotations

import sqlite3
import logging

log = logging.getLogger(__name__)


def run(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            id         TEXT PRIMARY KEY,
            applied_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
    """)
    conn.commit()
    _apply(conn, "001_fts_rebuild_standalone", _m001)


def _apply(conn: sqlite3.Connection, migration_id: str, fn):
    row = conn.execute(
        "SELECT id FROM _migrations WHERE id = ?", (migration_id,)
    ).fetchone()
    if row:
        return
    log.info("Migration: %s", migration_id)
    fn(conn)
    conn.execute("INSERT INTO _migrations (id) VALUES (?)", (migration_id,))
    conn.commit()
    log.info("Migration abgeschlossen: %s", migration_id)


def _m001(conn: sqlite3.Connection):
    """FTS-Tabelle von content= auf eigenständig umstellen.

    content= verlangt dass alle FTS-Spalten (filename, content) in der
    referenzierten Tabelle existieren — document_content hat aber kein
    filename-Feld. Die FTS-Tabelle wird daher als eigenständige Tabelle
    neu aufgebaut, die ihre eigenen Kopien speichert.
    """
    conn.executescript("""
        DROP TRIGGER IF EXISTS documents_fts_insert;
        DROP TRIGGER IF EXISTS documents_fts_update;
        DROP TRIGGER IF EXISTS documents_fts_delete;
        DROP TRIGGER IF EXISTS documents_fts_filename_insert;
        DROP TRIGGER IF EXISTS documents_fts_content_insert;
        DROP TRIGGER IF EXISTS documents_fts_content_update;
        DROP TRIGGER IF EXISTS documents_fts_content_delete;

        DROP TABLE IF EXISTS documents_fts;

        CREATE VIRTUAL TABLE documents_fts USING fts5(
            filename,
            content,
            tokenize='unicode61 remove_diacritics 2'
        );

        CREATE TRIGGER documents_fts_filename_insert
        AFTER INSERT ON documents BEGIN
            INSERT INTO documents_fts(rowid, filename, content)
            VALUES (new.id, new.filename, '');
        END;

        CREATE TRIGGER documents_fts_content_insert
        AFTER INSERT ON document_content BEGIN
            DELETE FROM documents_fts WHERE rowid = new.document_id;
            INSERT INTO documents_fts(rowid, filename, content)
            SELECT new.document_id, d.filename, new.content
            FROM documents d WHERE d.id = new.document_id;
        END;

        CREATE TRIGGER documents_fts_content_update
        AFTER UPDATE ON document_content BEGIN
            DELETE FROM documents_fts WHERE rowid = old.document_id;
            INSERT INTO documents_fts(rowid, filename, content)
            SELECT new.document_id, d.filename, new.content
            FROM documents d WHERE d.id = new.document_id;
        END;

        CREATE TRIGGER documents_fts_content_delete
        AFTER DELETE ON document_content BEGIN
            DELETE FROM documents_fts WHERE rowid = old.document_id;
            INSERT INTO documents_fts(rowid, filename, content)
            SELECT old.document_id, d.filename, ''
            FROM documents d WHERE d.id = old.document_id;
        END;
    """)

    # Alle bestehenden Dokumente mit ihrem Inhalt neu befüllen
    n = conn.execute("""
        INSERT INTO documents_fts(rowid, filename, content)
        SELECT d.id, d.filename, COALESCE(dc.content, '')
        FROM documents d
        LEFT JOIN document_content dc ON dc.document_id = d.id
    """).rowcount
    log.info("FTS neu aufgebaut: %d Dokumente indexiert", n)
