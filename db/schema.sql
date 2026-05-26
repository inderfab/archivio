PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Projekte des Architekturbüros
CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    path       TEXT    NOT NULL UNIQUE,
    active     INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Jede Datei, eindeutig über SHA256-Hash
-- extraction_status: pending | ok | error | unsupported
-- source_type: filesystem | email | archive
CREATE TABLE IF NOT EXISTS documents (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id         INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    hash               TEXT    NOT NULL UNIQUE,          -- SHA256
    filename           TEXT    NOT NULL,
    extension          TEXT    NOT NULL DEFAULT '',
    filesize           INTEGER NOT NULL DEFAULT 0,       -- Bytes
    modified_at        TEXT,                             -- ISO-8601, aus Dateisystem
    indexed_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    extraction_status  TEXT    NOT NULL DEFAULT 'pending'
                           CHECK (extraction_status IN ('pending', 'ok', 'error', 'unsupported')),
    source_type        TEXT    NOT NULL DEFAULT 'filesystem'
                           CHECK (source_type IN ('filesystem', 'email', 'archive'))
);

-- Eine Datei kann an mehreren Orten liegen (Duplikat, Spiegelung)
CREATE TABLE IF NOT EXISTS document_paths (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    path        TEXT    NOT NULL UNIQUE,
    is_primary  INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1))
);

-- Extrahierter Textinhalt; language z.B. 'de', 'en', ''
CREATE TABLE IF NOT EXISTS document_content (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
    content     TEXT    NOT NULL DEFAULT '',
    language    TEXT    NOT NULL DEFAULT ''
);

-- E-Mail-Metadaten (source_type = 'email')
CREATE TABLE IF NOT EXISTS mails (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
    sender      TEXT    NOT NULL DEFAULT '',
    recipients  TEXT    NOT NULL DEFAULT '',  -- JSON-Array als Text
    subject     TEXT    NOT NULL DEFAULT '',
    date        TEXT,                         -- ISO-8601
    thread_id   TEXT                          -- Message-ID des Threads
);

-- FTS5 Volltextsuche über Dateinamen + Inhalt
-- content= macht es zu einem "content table" (kein Datenkopie, Trigger nötig)
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    filename,
    content,
    content=document_content,
    content_rowid=document_id,
    tokenize='unicode61 remove_diacritics 2'
);

-- Trigger: FTS nach INSERT in document_content befüllen
CREATE TRIGGER IF NOT EXISTS documents_fts_insert
AFTER INSERT ON document_content BEGIN
    INSERT INTO documents_fts(rowid, filename, content)
    SELECT new.document_id, d.filename, new.content
    FROM documents d WHERE d.id = new.document_id;
END;

-- Trigger: FTS bei UPDATE aktualisieren
CREATE TRIGGER IF NOT EXISTS documents_fts_update
AFTER UPDATE ON document_content BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, filename, content)
    VALUES ('delete', old.document_id, '', '');
    INSERT INTO documents_fts(rowid, filename, content)
    SELECT new.document_id, d.filename, new.content
    FROM documents d WHERE d.id = new.document_id;
END;

-- Trigger: FTS bei DELETE bereinigen
CREATE TRIGGER IF NOT EXISTS documents_fts_delete
AFTER DELETE ON document_content BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, filename, content)
    VALUES ('delete', old.document_id, '', '');
END;

-- Indices
CREATE INDEX IF NOT EXISTS idx_documents_project    ON documents(project_id);
CREATE INDEX IF NOT EXISTS idx_documents_extension  ON documents(extension);
CREATE INDEX IF NOT EXISTS idx_documents_status     ON documents(extraction_status);
CREATE INDEX IF NOT EXISTS idx_document_paths_doc   ON document_paths(document_id);
CREATE INDEX IF NOT EXISTS idx_mails_thread         ON mails(thread_id);
CREATE INDEX IF NOT EXISTS idx_mails_date           ON mails(date);
