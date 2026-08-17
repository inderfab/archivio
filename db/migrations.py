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
    _apply(conn, "006_mails_mailbox_name", _m006)
    _apply(conn, "007_extraction_status_listed", _m007)
    _apply(conn, "008_fts_doc_delete_trigger", _m008)
    _apply(conn, "009_projects_last_scanned_at", _m009)
    _apply(conn, "010_photo_ratings", _m010)
    _apply(conn, "011_photo_tags", _m011)


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


def _m006(conn: sqlite3.Connection):
    """mails.mailbox_name — Herkunfts-Postfach für nicht zugewiesene Mails."""
    try:
        conn.execute("ALTER TABLE mails ADD COLUMN mailbox_name TEXT NOT NULL DEFAULT ''")
        conn.commit()
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            raise


def _m007(conn: sqlite3.Connection):
    """extraction_status: zusätzlichen Wert 'listed' erlauben.

    Bisher kannte die CHECK-Constraint nur ('pending','ok','error','unsupported').
    Der Scanner schreibt für List-Only-Dateien (Bilder, Video, grosse PDFs,
    unbekannte Formate) aber 'listed' — das schlug an der Constraint fehl, die
    gesamte Transaktion (Dokument + Pfad) wurde zurückgerollt und die Datei
    landete nie in der DB. SQLite kann CHECK-Constraints nicht per ALTER ändern;
    daher wird die gespeicherte Tabellen-DDL über writable_schema gepatcht.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents'"
    ).fetchone()
    if not row or not row[0]:
        return
    sql = row[0]
    if "'listed'" in sql:
        return  # bereits aktualisiert

    import re
    new_sql = re.sub(
        r"CHECK\s*\(\s*extraction_status\s+IN\s*\(([^)]*)\)\s*\)",
        lambda m: "CHECK (extraction_status IN (" + m.group(1).strip() + ", 'listed'))",
        sql, count=1,
    )
    if new_sql == sql or "'listed'" not in new_sql \
            or not new_sql.lstrip().upper().startswith("CREATE TABLE"):
        log.warning("m007: CHECK-Constraint nicht gefunden/ungültig — übersprungen")
        return

    conn.execute("PRAGMA writable_schema = ON")
    try:
        conn.execute(
            "UPDATE sqlite_master SET sql = ? WHERE type='table' AND name='documents'",
            (new_sql,),
        )
        conn.commit()
    finally:
        conn.execute("PRAGMA writable_schema = OFF")
    log.info("m007: extraction_status erlaubt jetzt 'listed'")


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


def _m008(conn: sqlite3.Connection):
    """FTS-Eintrag entfernen wenn das Dokument selbst gelöscht wird.

    Bisher fehlte ein AFTER DELETE ON documents-Trigger. Bei Dokumenten ohne
    document_content-Zeile (Bilder, Fehler, pending) blieb beim Löschen ein
    verwaister documents_fts-Eintrag zurück → veraltete Dateinamen-Treffer.
    """
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS documents_fts_doc_delete
        AFTER DELETE ON documents BEGIN
            DELETE FROM documents_fts WHERE rowid = old.id;
        END
    """)
    conn.commit()


def _m009(conn: sqlite3.Connection):
    """projects.last_scanned_at — Zeitpunkt des letzten Scans (auch wenn nur
    übersprungen wurde). MAX(indexed_at) der Dokumente ist dafür ungeeignet, weil
    es bei Skip-only-Scans unverändert bleibt → Nutzer denkt fälschlich, es sei
    nicht gescannt worden."""
    try:
        conn.execute("ALTER TABLE projects ADD COLUMN last_scanned_at TEXT")
        conn.commit()
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            raise


def _m010(conn: sqlite3.Connection):
    """Sternebewertung für Fotos (Foto-Browser, 1-5 Sterne, dokumentweit/global).

    Hängt am Dokument (Hash), nicht am Pfad: Archivios Dateiidentität ist der
    SHA256-Hash. Ein Foto in mehreren Projektordnern ist bewertungsmässig überall
    dasselbe. Keine "unbewertet"-Zeile (0) -- Entfernen der Bewertung löscht die
    Zeile statt sie auf 0 zu setzen.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS photo_ratings (
            document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            rating      INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
            rated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
    """)
    conn.commit()


def _m011(conn: sqlite3.Connection):
    """Globale Tags für Fotos, ordnerübergreifend (Foto-Browser).

    Wie die Sternebewertung dokumentweit (Hash), nicht pfadweit. Tags sind global
    über alle Projekte -- der Projektfilter schränkt beim Suchen/Filtern zusätzlich
    ein, ist aber keine Voraussetzung fürs Taggen selbst.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS photo_tags (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        CREATE TABLE IF NOT EXISTS photo_tag_assignments (
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            tag_id      INTEGER NOT NULL REFERENCES photo_tags(id) ON DELETE CASCADE,
            PRIMARY KEY (document_id, tag_id)
        );
        CREATE INDEX IF NOT EXISTS idx_photo_tag_assignments_tag ON photo_tag_assignments(tag_id);
    """)
    conn.commit()
