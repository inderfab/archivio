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
    _apply(conn, "012_norms", _m012)
    _apply(conn, "013_mcp_log", _m013)
    _apply(conn, "014_mcp_whitelist", _m014)
    _apply(conn, "015_block_rules", _m015)
    _apply(conn, "016_scan_log", _m016)
    _apply(conn, "017_mail_mcp_enabled", _m017)
    _apply(conn, "018_mcp_log_session", _m018)
    _apply(conn, "019_scan_log_batch", _m019)
    _apply(conn, "020_norm_freshness", _m020)
    _apply(conn, "021_search_log", _m021)
    _apply(conn, "022_search_log_token", _m022)


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


def _m012(conn: sqlite3.Connection):
    """Norm-Erkennung: technische Normen (SIA/VSS/EN/DIN/ISO) werden markiert, ihr
    Volltext verlässt Archivio nicht über MCP (Urheber-/Lizenzrecht der Herausgeber).
    Siehe scanner/norms.py und config/norms.yaml.

    Spalten auf documents, nicht eigene Tabelle -- anders als photo_ratings/-tags:
    hier geht es um eine Eigenschaft JEDES Dokuments, die bei JEDER Suchabfrage
    gebraucht wird (Redaktion auf dem heissen MCP-Pfad). Ein LEFT JOIN wäre
    unnötiger Aufwand.

    norm_manual=1: von Hand gesetzt/aufgehoben -- der Rescan (scanner/norms.py)
    fasst solche Zeilen nie an, sonst geht jede Korrektur beim nächsten Scan verloren.

    norm_folders: gelernte, vom Nutzer bestätigte Norm-Ordner (büro-spezifisch,
    NICHT Teil von config/norms.yaml). status='confirmed' aktiviert Layer 2 der
    Klassifikation (Ordnerregel) auch für noch ungescannte/textlose Dateien.
    """
    for stmt in [
        "ALTER TABLE documents ADD COLUMN is_norm INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE documents ADD COLUMN norm_reason TEXT",
        "ALTER TABLE documents ADD COLUMN norm_manual INTEGER NOT NULL DEFAULT 0",
    ]:
        try:
            conn.execute(stmt)
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                raise
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_documents_is_norm ON documents(is_norm);

        CREATE TABLE IF NOT EXISTS norm_folders (
            path        TEXT PRIMARY KEY,
            status      TEXT NOT NULL CHECK(status IN ('proposed','confirmed','rejected')),
            n_docs      INTEGER NOT NULL DEFAULT 0,
            n_norms     INTEGER NOT NULL DEFAULT 0,
            detected_at TEXT,
            decided_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_norm_folders_status ON norm_folders(status);
    """)
    conn.commit()


def _m013(conn: sqlite3.Connection):
    """Protokoll aller Datenübermittlungen über die MCP-Schnittstelle (search,
    semantic_search, document) -- Grundlage für die Seite /mcp-log ("Claude-Zugriffe").
    Siehe scanner/mcp_log.py. Wird selbst nie über MCP abgefragt.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mcp_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            tool         TEXT NOT NULL,
            query        TEXT,
            project_id   INTEGER,
            files_json   TEXT NOT NULL DEFAULT '[]',
            chars_sent   INTEGER NOT NULL DEFAULT 0,
            blocked_json TEXT NOT NULL DEFAULT '[]',
            status       TEXT NOT NULL DEFAULT 'ok'
        );
        CREATE INDEX IF NOT EXISTS idx_mcp_log_ts ON mcp_log(ts);
    """)
    conn.commit()


def _m014(conn: sqlite3.Connection):
    """MCP-Zugriff auf ein Freigabemodell umgestellt: standardmässig hat MCP auf KEIN
    Projekt Zugriff, Projekte werden explizit im Dashboard freigegeben (siehe
    web/dashboard.py, _dashboard_projects.html). DEFAULT 0 setzt bestehende
    Installationen beim Update automatisch auf "nichts freigegeben" -- keine
    separate Reset-Logik nötig, das ist hier bereits das gewünschte Verhalten.
    """
    try:
        conn.execute("ALTER TABLE projects ADD COLUMN mcp_enabled INTEGER NOT NULL DEFAULT 0")
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            raise
    conn.commit()


def _m015(conn: sqlite3.Connection):
    """Manuelle Sperrliste für MCP -- ergänzt die automatische Norm-Erkennung
    (scanner/norms.py) um von Hand gesetzte Regeln: einzelne Dateien (per Hash,
    überlebt Verschiebungen), Namens-Muster (Glob), Ordner, ganze Projekte.
    Siehe scanner/block_list.py.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS block_rules (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            type       TEXT NOT NULL CHECK(type IN ('file','pattern','folder','project')),
            value      TEXT NOT NULL,
            label      TEXT,
            enabled    INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_block_rules_enabled ON block_rules(enabled);
    """)
    conn.commit()


def _m016(conn: sqlite3.Connection):
    """Protokoll jedes Projekt-Scans: Dauer, Datei-Zähler, Fehler mit Pfad, Spitzenwerte
    bei Speicher-/CPU-Nutzung -- Grundlage für die Seite /system-status. Siehe
    scanner/scan_log.py und web/dashboard.py::_run_scan().
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scan_log (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id     INTEGER,
            project_name   TEXT,
            started_at     TEXT NOT NULL,
            finished_at    TEXT,
            duration_s     REAL,
            status         TEXT NOT NULL,
            total          INTEGER NOT NULL DEFAULT 0,
            processed      INTEGER NOT NULL DEFAULT 0,
            new_count      INTEGER NOT NULL DEFAULT 0,
            skipped        INTEGER NOT NULL DEFAULT 0,
            error_count    INTEGER NOT NULL DEFAULT 0,
            errors_json    TEXT NOT NULL DEFAULT '[]',
            peak_memory_mb REAL,
            peak_cpu_pct   REAL
        );
        CREATE INDEX IF NOT EXISTS idx_scan_log_started ON scan_log(started_at);
    """)
    conn.commit()


def _m017(conn: sqlite3.Connection):
    """MCP-Freigabe für Postfächer, die (noch) keinem Projekt zugeordnet sind --
    deren Mails haben documents.project_id NULL und würden sonst NIE über MCP
    erreichbar sein (projects.mcp_enabled greift dort nicht, es gibt kein Projekt).
    Ist ein Postfach mit einem Projekt verknüpft, übernimmt es dessen mcp_enabled
    und dieses Flag wird ignoriert -- siehe web/api.py::_mcp_allowed_doc_ids().
    """
    try:
        conn.execute("ALTER TABLE mail_scan_config ADD COLUMN mcp_enabled INTEGER NOT NULL DEFAULT 0")
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            raise
    conn.commit()


def _m018(conn: sqlite3.Connection):
    """Session-Kennung pro MCP-Log-Zeile -- helper/archivio_mcp.py generiert einmal
    pro Subprozess-Start (i.d.R. einmal pro Claude-Desktop-Verbindung, solange der
    Connector aktiv bleibt) eine kurze zufällige ID und schickt sie bei jedem
    Tool-Aufruf mit. Ohne das erzeugt eine einzelne Nutzerfrage, bei der Claude
    mehrfach sucht/nachlädt, ebenso viele einzelne, unzusammenhängend wirkende
    Protokollzeilen -- siehe web/main.py::_group_mcp_log_entries() fürs Gruppieren
    in der Anzeige. Zeilen ohne session_id (vor diesem Update oder von einem
    direkten HTTP-Aufruf ohne den Parameter) bleiben einzeln, das ist beabsichtigt.
    """
    try:
        conn.execute("ALTER TABLE mcp_log ADD COLUMN session_id TEXT")
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            raise
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mcp_log_session ON mcp_log(session_id)")
    conn.commit()


def _m019(conn: sqlite3.Connection):
    """Batch-Kennung pro Scan-Protokoll-Zeile -- web/api.py::scan_all() erzeugt einmal
    pro "Alle scannen"-Lauf (Klick oder naechtlicher Scheduler) eine kurze zufaellige
    ID und reicht sie an jeden Projekt-Scan durch. Ohne das erscheint ein einzelner
    Sammel-Scan ueber z.B. 30 Projekte als 30 unzusammenhaengende Einzelzeilen im
    Systemstatus -- siehe web/main.py::_group_scan_log_entries() fuers Gruppieren in
    der Anzeige. Zeilen ohne batch_id (Einzel-Scan eines Projekts) bleiben einzeln,
    das ist beabsichtigt."""
    try:
        conn.execute("ALTER TABLE scan_log ADD COLUMN batch_id TEXT")
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            raise
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scan_log_batch ON scan_log(batch_id)")
    conn.commit()


def _m020(conn: sqlite3.Connection):
    """Gültigkeitsdatum + Aktualitäts-Status pro erkannter Norm (scanner/norms.py::
    extract_valid_from(), scanner/norm_freshness.py). norm_check_status ist
    bewusst NIE automatisch/periodisch gesetzt -- nur per Button auf /norms
    (Einzeln oder "Alle prüfen"), da der Abgleich externe Shop-Websites (SIA/VSS)
    kontaktiert, was ausserhalb des sonst rein lokalen Prinzips von Archivio liegt
    und explizit auf Zuruf laufen soll, nicht im Hintergrund."""
    try:
        conn.execute("ALTER TABLE documents ADD COLUMN norm_valid_from TEXT")
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            raise
    try:
        conn.execute("ALTER TABLE documents ADD COLUMN norm_check_status TEXT NOT NULL DEFAULT 'ungeprüft'")
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            raise
    try:
        conn.execute("ALTER TABLE documents ADD COLUMN norm_checked_at TEXT")
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            raise
    conn.commit()


def _m021(conn: sqlite3.Connection):
    """Protokoll jeder Websuche (normale Suche und KI-Suche) -- Anfrage, Trefferzahl,
    Dauer, und ob danach tatsächlich ein Treffer geöffnet wurde (clicks). Bewusst
    OHNE Bezug zu Person oder Gerät -- anders als mcp_log (dessen Zweck gerade die
    Nachvollziehbarkeit ist), dient dieses Protokoll nur der Suchqualität selbst
    (z.B. häufige Anfragen ohne Treffer oder ohne Klick). Grundlage für den
    Abschnitt "Suche-Protokoll" auf /system-status. Siehe scanner/search_log.py.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS search_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            kind         TEXT NOT NULL,
            query        TEXT,
            project_id   INTEGER,
            filters      TEXT,
            result_count INTEGER NOT NULL DEFAULT 0,
            duration_ms  INTEGER NOT NULL DEFAULT 0,
            clicks       INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_search_log_ts ON search_log(ts);
    """)
    conn.commit()


def _m022(conn: sqlite3.Connection):
    """Kennung pro Tippvorgang (ein zufälliger Wert pro Seitenaufruf, siehe
    index.html #search_token) -- die Live-Suche feuert bei jedem Tastenanschlag
    (300ms Debounce), was ohne das hier für ein einziges Wort mehrere Zeilen im
    Suche-Protokoll erzeugt ("ne", "net", "netzwerk"). scanner/search_log.py
    aktualisiert bei gleichem token + noch keinem Klick + innerhalb weniger
    Sekunden dieselbe Zeile statt eine neue anzulegen -- ein Klick beendet das
    Zusammenfassen (die Zeile gilt dann als abgeschlossen), ein längeres
    Zeitfenster zwischen zwei Anfragen ebenfalls (neue eigenständige Suche)."""
    try:
        conn.execute("ALTER TABLE search_log ADD COLUMN token TEXT")
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            raise
    conn.execute("CREATE INDEX IF NOT EXISTS idx_search_log_token ON search_log(token)")
    conn.commit()
