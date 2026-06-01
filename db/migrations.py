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
    _apply(conn, "002_ignored_paths", _m002)
    _apply(conn, "003_mail_integration", _m003)
    _apply(conn, "004_add_chunks", _m004)
    _apply(conn, "005_chunk_doc_index", _m005)


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


def _m005(conn: sqlite3.Connection):
    """Index auf document_chunks(document_id) — macht per-Doc-Queries O(log n) statt O(n)."""
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_document_chunks_doc
        ON document_chunks(document_id)
    """)
    log.info("Index idx_document_chunks_doc erstellt")


def _m001(conn: sqlite3.Connection):
    """FTS-Tabelle von content= auf eigenständig umstellen."""
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

    n = conn.execute("""
        INSERT INTO documents_fts(rowid, filename, content)
        SELECT d.id, d.filename, COALESCE(dc.content, '')
        FROM documents d
        LEFT JOIN document_content dc ON dc.document_id = d.id
    """).rowcount
    log.info("FTS neu aufgebaut: %d Dokumente indexiert", n)


def _m002(conn: sqlite3.Connection):
    """ignored_paths Tabelle anlegen."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ignored_paths (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            path       TEXT    NOT NULL,
            created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            UNIQUE(project_id, path)
        );
        CREATE INDEX IF NOT EXISTS idx_ignored_paths_project ON ignored_paths(project_id);
    """)


def _m004(conn: sqlite3.Connection):
    """Chunk-Tabelle und chunks_fts für seitenbasiertes Chunking."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS document_chunks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            page_number INTEGER,
            chunk_index INTEGER,
            content     TEXT,
            embedding   BLOB,
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            content,
            content='document_chunks',
            content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        );

        CREATE TRIGGER IF NOT EXISTS chunks_fts_insert AFTER INSERT ON document_chunks BEGIN
            INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS chunks_fts_delete AFTER DELETE ON document_chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES ('delete', old.id, old.content);
        END;
        CREATE TRIGGER IF NOT EXISTS chunks_fts_update AFTER UPDATE ON document_chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES ('delete', old.id, old.content);
            INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
        END;
    """)
    log.info("Chunks-Tabelle und chunks_fts angelegt")


def _m003(conn: sqlite3.Connection):
    """Mail-Integration: metadata/cc-Spalten + mail_scan_config."""
    for stmt in [
        "ALTER TABLE documents ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE mails ADD COLUMN cc TEXT NOT NULL DEFAULT ''",
        """CREATE TABLE IF NOT EXISTS mail_scan_config (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id      INTEGER REFERENCES projects(id),
            mailbox_name    TEXT    NOT NULL UNIQUE,
            active          INTEGER NOT NULL DEFAULT 0,
            last_scanned_at TEXT,
            mail_count      INTEGER NOT NULL DEFAULT 0
        )""",
    ]:
        try:
            conn.execute(stmt)
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                raise
    conn.commit()
