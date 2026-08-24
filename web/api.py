"""JSON-API."""
from __future__ import annotations

import base64
import io
import os
import re
import subprocess
import threading
import zipfile
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from config import settings
from db import connection
from web.dashboard import _mail_scan, _run_mail_scan, _run_scan, _scans, _cancel_flags, _now

router = APIRouter(prefix="/api")

_VERSION_FILE = Path(__file__).parent.parent / "VERSION"
_HELPER_VERSION_FILE = Path(__file__).parent.parent / "HELPER_VERSION"


@router.get("/version")
async def version():
    v = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "0.0.0"
    # Helper-Version separat — der Helper vergleicht dagegen, nicht gegen die
    # Server-Version. So loest ein Server-Update kein Helper-Update aus.
    hv = _HELPER_VERSION_FILE.read_text().strip() if _HELPER_VERSION_FILE.exists() else v
    return JSONResponse({"version": v, "helper_version": hv})


@router.get("/status")
async def status():
    conn  = connection.get_connection()
    total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    last  = conn.execute("SELECT MAX(indexed_at) FROM documents").fetchone()[0]
    conn.close()

    base_folders = settings.get("scanner.base_folders", [])
    nas_ok  = any(Path(f.get("path", "")).exists() for f in base_folders)
    nas_path = base_folders[0].get("path", "") if base_folders else ""

    return JSONResponse({
        "server":    True,
        "nas":       nas_ok,
        "nas_path":  nas_path,
        "doc_count": total,
        "last_scan": last,
    })


@router.get("/mcp/search")
async def mcp_search(
    q: str = "",
    project_id: str = "",
    search_in: str = "docs,filenames,folders",
    limit: int = 20,
):
    """Read-only JSON-Suche für den MCP-Server (Archivio Helper) — reine Textantwort statt HTML.
    Nutzt dieselbe Such-Logik wie /search (main.py), nur ohne die Zusatzfilter (Datum, Absender, …).

    Läuft über run_in_executor: _run_scoped_search ist eine blockierende SQLite-Anfrage,
    die bei Mehrwort-Queries mit vielen FTS-OR-Zweigen auf grossen Indizes mehrere Sekunden
    dauern kann. Direkt im async-Handler ausgefuehrt wuerde das den einzigen uvicorn-
    Worker/Event-Loop komplett blockieren -- ALLE anderen gleichzeitigen Anfragen (auch
    von anderen Nutzern, auch list_folder desselben MCP-Clients) haengen so lange fest,
    was sich als scheinbar zufaellige Timeouts aeussert obwohl der Server nur mit genau
    dieser einen Suche beschaeftigt ist."""
    import asyncio

    from web.main import _build_filters, _prefer_project_path, _run_scoped_search, _search_folders

    q = q.strip()
    scope = set(search_in.split(",")) if search_in else {"docs", "filenames", "folders"}

    def _do_search():
        conn = connection.get_connection()
        try:
            results: list = []
            if q:
                filters_str, filter_params = _build_filters(project_id, "")
                results, _error = _run_scoped_search(conn, q, filters_str, filter_params, scope)
                _prefer_project_path(conn, results, project_id)
            folders = _search_folders(conn, q, project_id) if q and "folders" in scope else []
            return results, folders
        finally:
            conn.close()

    loop = asyncio.get_event_loop()
    results, folders = await loop.run_in_executor(None, _do_search)

    cleaned = [
        {
            "id":            r.get("id"),
            "filename":      r.get("filename"),
            "extension":     r.get("extension"),
            "filesize":      r.get("filesize"),
            "modified_at":   r.get("modified_at"),
            "project_name":  r.get("project_name"),
            "filepath":      r.get("filepath"),
            "page_number":   r.get("page_number"),
            "mail_sender":   r.get("mail_sender"),
            "mail_date":     r.get("mail_date"),
            # <mark>-Tags entfernen — reiner Text statt HTML-Highlighting fürs LLM
            "excerpt":       re.sub(r"</?mark>", "", r.get("excerpt") or ""),
        }
        for r in results[:limit]
    ]
    return JSONResponse({"results": cleaned, "folders": folders})


@router.get("/mcp/semantic-search")
async def mcp_semantic_search(
    q: str = "",
    project_id: str = "",
    limit: int = 12,
):
    """Hybrid keyword+vector Suche (wie /search/ai), liefert Chunk-Inhalte statt HTML —
    Claude formuliert die Antwort selbst aus den Quellen, kein lokaler LLM-Aufruf nötig.
    """
    import asyncio

    from web.main import _ai_vector_search

    q = q.strip()
    if not q:
        return JSONResponse({"sources": [], "error": None, "ollama_missing": False})

    loop = asyncio.get_event_loop()
    sources, error, ollama_missing = await loop.run_in_executor(
        None, _ai_vector_search, q, project_id
    )

    cleaned = [
        {
            "document_id":  s.get("document_id"),
            "filename":     s.get("filename"),
            "project_name": s.get("project_name"),
            "filepath":     s.get("filepath"),
            "page_number":  s.get("page_number"),
            "content":      s.get("content"),
            "score":        s.get("score"),
            "match_type":   s.get("match_type"),
        }
        for s in (sources or [])[:limit]
    ]
    return JSONResponse({"sources": cleaned, "error": error, "ollama_missing": ollama_missing})


@router.get("/mcp/document")
async def mcp_document(document_id: int):
    """Volltext + Metadaten eines Dokuments — damit der MCP-Server (read_document) den
    Inhalt in die Claude-Unterhaltung laden kann (z.B. Mail-Text zum Umschreiben)."""
    conn = connection.get_connection()
    try:
        doc = conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if not doc:
            return JSONResponse({"error": f"Kein Dokument mit id {document_id}"}, status_code=404)
        doc = dict(doc)
        content_row = conn.execute(
            "SELECT content FROM document_content WHERE document_id=?", (document_id,)
        ).fetchone()
        path_row = conn.execute(
            "SELECT path FROM document_paths WHERE document_id=? AND is_primary=1", (document_id,)
        ).fetchone()
        result = {
            "id":                document_id,
            "filename":          doc["filename"],
            "extension":         doc["extension"],
            "source_type":       doc["source_type"],
            "extraction_status": doc["extraction_status"],
            "filepath":          path_row["path"] if path_row else None,
            "content":           (content_row["content"] if content_row else "") or "",
        }
        if doc["source_type"] == "email":
            m = conn.execute(
                "SELECT sender, recipients, cc, subject, date, mailbox_name "
                "FROM mails WHERE document_id=?", (document_id,)
            ).fetchone()
            if m:
                result["mail"] = dict(m)
        return JSONResponse(result)
    finally:
        conn.close()


@router.get("/mcp/base-folders")
async def mcp_base_folders():
    """Gibt die konfigurierten NAS-Wurzelpfade zurück — der MCP-Server (list_folder)
    prüft damit lokal, dass ein angefragter Pfad innerhalb eines erlaubten Bereichs
    liegt, bevor er ihn auflistet. Reine Config-Auskunft, kein Filesystem-Zugriff hier."""
    base_folders = settings.get("scanner.base_folders", [])
    folders = [f.get("path") for f in base_folders if f.get("path")]
    return JSONResponse({"folders": folders})


@router.post("/scan/all")
async def scan_all():
    conn     = connection.get_connection()
    # Stale-first: am laengsten nicht gescannte Projekte zuerst (NULL = nie
    # gescannt sortiert in SQLite ASC ganz nach vorne). So kommt nach einem
    # RAM-Neustart der Batch mit den NOCH offenen Projekten weiter, statt immer
    # wieder bei denselben (grossen) Projekten von vorne zu beginnen.
    projects = conn.execute(
        "SELECT id, name, path FROM projects WHERE active=1 ORDER BY last_scanned_at ASC"
    ).fetchall()
    conn.close()

    started = 0
    for p in projects:
        if _scans.get(p["id"], {}).get("status") == "running":
            continue
        # Vollständiger Zustand wie beim Einzel-Scan — sonst fehlen dem Banner
        # Projektname/-pfad und den Zählern die Startwerte.
        _scans[p["id"]] = {
            "status":       "running",
            "phase":        "collecting",
            "project_name": p["name"],
            "project_root": p["path"],
            "total":        0,
            "processed":    0,
            "new":          0,
            "skipped":      0,
            "listed":       0,
            "errors":       0,
            "current_file":   "",
            "current_folder": "",
            "started_at":     _now(),
        }
        _cancel_flags[p["id"]] = {"cancel": False}
        # scan_mail=False: verknüpfte Postfächer deckt der globale Mail-Scan unten ab
        threading.Thread(
            target=_run_scan, args=(p["id"], p["path"]), kwargs={"scan_mail": False}, daemon=True
        ).start()
        started += 1

    if _mail_scan.get("status") != "running":
        _mail_scan.clear()
        _mail_scan["status"]     = "running"
        _mail_scan["started_at"] = _now()
        threading.Thread(target=_run_mail_scan, daemon=True).start()

    return JSONResponse({"ok": True, "projects_started": started})


@router.post("/scan/cancel-all")
async def cancel_all_scans():
    """Bricht ALLE Projekt-Scans (laufend UND in der Warteschlange) sowie den
    Mail-Scan auf einmal ab. Laufende Scans prüfen ihr Cancel-Flag periodisch;
    wartende (queued) Threads sehen es, sobald sie den Scan-Lock bekommen, und
    kehren dann sofort zurück (ohne wirklich zu scannen)."""
    cancelled = 0
    for pid, flag in list(_cancel_flags.items()):
        flag["cancel"] = True
    for pid, s in list(_scans.items()):
        # 'running' deckt sowohl laufende als auch wartende (phase 'queued') Scans ab —
        # beide tragen status 'running'.
        if s.get("status") == "running":
            s["status"]      = "cancelled"
            s["finished_at"] = _now()
            cancelled += 1
    # Mail-Scan ebenfalls stoppen (Flag wird zwischen den Postfächern geprüft)
    mail_cancelled = False
    if _mail_scan.get("status") == "running":
        _mail_scan["cancel"] = True
        mail_cancelled = True

    return JSONResponse({"ok": True, "cancelled": cancelled, "mail_cancelled": mail_cancelled})


@router.get("/scan/nav-status", response_class=None)
async def scan_nav_status():
    """Mini-Indikator für die globale Navigation — leer wenn kein Scan aktiv."""
    from fastapi.responses import HTMLResponse as _HTML

    # Dokument-Scan
    running = [s for s in _scans.values() if s.get("status") == "running"]
    if running:
        s         = running[0]
        total     = s.get("total", 0)
        processed = s.get("processed", 0)
        label     = "⟳ Scan läuft"
        return _HTML(f'<span class="nav-scan-pill">{label}</span>')

    # Mail-Scan
    if _mail_scan.get("status") == "running":
        done  = _mail_scan.get("processed", 0)
        total = _mail_scan.get("total", 0)
        if total > 0:
            label = f"⟳ Mail-Scan {done}/{total}"
        else:
            label = "⟳ Mail-Scan läuft…"
        return _HTML(f'<span class="nav-scan-pill">{label}</span>')

    return _HTML("")


@router.get("/debug/page-number-stats")
async def page_number_stats():
    """Zeigt Verteilung von page_number in document_chunks und was FTS-Suche zurückgibt."""
    conn = connection.get_connection()
    try:
        with_page = conn.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE page_number IS NOT NULL AND page_number > 0"
        ).fetchone()[0]
        without_page = conn.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE page_number IS NULL OR page_number = 0"
        ).fetchone()[0]
        # Beispiel-FTS-Suche: erste 5 Treffer mit page_number
        sample = conn.execute("""
            SELECT d.filename, dc.page_number, dc.content
            FROM chunks_fts cf
            JOIN document_chunks dc ON cf.rowid = dc.id
            JOIN documents d ON d.id = dc.document_id
            WHERE chunks_fts MATCH 'Brandschutz'
            LIMIT 5
        """).fetchall()
        return {
            "chunks_with_page_number": with_page,
            "chunks_without_page_number": without_page,
            "fts_sample_brandschutz": [
                {"filename": r[0], "page_number": r[1], "content_preview": (r[2] or "")[:80]}
                for r in sample
            ]
        }
    finally:
        conn.close()


@router.get("/debug/fts-state")
async def fts_state():
    """Prüft ob chunks_fts befüllt ist und Trigger existieren."""
    conn = connection.get_connection()
    try:
        chunks_total = conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
        fts_total    = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
        triggers     = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'chunks_fts%'"
        ).fetchall()]
        return {
            "document_chunks": chunks_total,
            "chunks_fts":      fts_total,
            "fts_in_sync":     chunks_total == fts_total,
            "triggers":        triggers,
        }
    finally:
        conn.close()


@router.post("/fix/rebuild-fts")
async def rebuild_fts():
    """Baut chunks_fts komplett neu aus document_chunks auf."""
    def _rebuild():
        conn = connection.get_connection()
        try:
            # Trigger temporär deaktivieren
            conn.execute("DROP TRIGGER IF EXISTS chunks_fts_insert")
            conn.execute("DROP TRIGGER IF EXISTS chunks_fts_delete")
            conn.execute("DROP TRIGGER IF EXISTS chunks_fts_update")
            # FTS leeren und neu befüllen
            conn.execute("DELETE FROM chunks_fts")
            conn.execute("INSERT INTO chunks_fts(rowid, content) SELECT id, content FROM document_chunks WHERE content IS NOT NULL")
            conn.commit()
            # Trigger neu anlegen
            conn.executescript("""
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
            count = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
            return count
        finally:
            conn.close()
    import asyncio
    count = await asyncio.get_event_loop().run_in_executor(None, _rebuild)
    return {"ok": True, "chunks_fts_rebuilt": count}


@router.get("/scan/state")
async def scan_state():
    """Gibt den aktuellen Scan-State zurück (für Diagnose)."""
    conn = connection.get_connection()
    projects = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM projects").fetchall()}
    conn.close()
    return {
        "scans": {
            projects.get(pid, str(pid)): {k: v for k, v in s.items() if k != "request"}
            for pid, s in _scans.items()
        },
        "mail_scan": {k: v for k, v in _mail_scan.items()},
    }


@router.post("/reset-and-rescan")
async def reset_and_rescan():
    """Löscht alle Chunks/Embeddings, setzt alle Dokumente auf 'pending' und startet Scan.
    Der Scanner extrahiert, chunked und embeddet alles neu — korrekt von Anfang an.
    """
    def _reset():
        conn = connection.get_connection()
        try:
            # FTS-Triggers deaktivieren damit DELETE schneller geht
            # (sonst feuert der Trigger für jeden der 24k Chunks einzeln)
            conn.execute("DROP TRIGGER IF EXISTS chunks_fts_delete")
            conn.execute("DROP TRIGGER IF EXISTS chunks_fts_insert")
            conn.execute("DROP TRIGGER IF EXISTS chunks_fts_update")
            # Alle Chunks löschen
            conn.execute("DELETE FROM document_chunks")
            # FTS direkt leeren (kein Trigger mehr)
            try:
                conn.execute("DELETE FROM chunks_fts")
            except Exception:
                pass
            # Alle Dokumente auf pending setzen
            conn.execute("UPDATE documents SET extraction_status = 'pending', content = NULL")
            # Versuche language-Spalte zu leeren falls sie existiert
            try:
                conn.execute("UPDATE documents SET language = NULL")
            except Exception:
                pass
            conn.commit()
            # Triggers neu anlegen
            conn.executescript("""
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
        finally:
            conn.close()

    import asyncio
    await asyncio.get_event_loop().run_in_executor(None, _reset)

    # Scanner für alle aktiven Projekte starten
    conn = connection.get_connection()
    projects = conn.execute("SELECT id, path FROM projects WHERE active=1").fetchall()
    conn.close()
    for p in projects:
        if _scans.get(p["id"], {}).get("status") == "running":
            continue
        _scans[p["id"]] = {"status": "running", "started_at": _now()}
        threading.Thread(target=_run_scan, args=(p["id"], p["path"]),
                         kwargs={"scan_mail": False}, daemon=True).start()

    return JSONResponse({
        "ok": True,
        "message": f"Reset abgeschlossen. {len(projects)} Projekt(e) werden neu gescannt und eingebettet."
    })


@router.get("/debug/extract-error")
async def debug_extract_error(filename: str = ""):
    """Versucht eine Datei zu extrahieren und gibt den genauen Fehler zurück."""
    if not filename:
        return JSONResponse({"error": "filename parameter required"}, status_code=400)
    conn = connection.get_connection()
    try:
        row = conn.execute("""
            SELECT dp.path FROM documents d
            JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
            WHERE d.filename LIKE ? AND d.extraction_status = 'error'
            LIMIT 1
        """, (f"%{filename}%",)).fetchone()
        if not row:
            return JSONResponse({"error": f"Keine error-Datei gefunden mit: {filename}"})
        path = row[0]
    finally:
        conn.close()

    import traceback
    from pathlib import Path
    result = {"path": path, "fitz": None, "ocr": None, "pypdf": None}

    # Test 1: fitz.open
    try:
        import fitz
        doc = fitz.open(path)
        result["fitz"] = f"ok — {doc.page_count} Seiten, encrypted={doc.is_encrypted}, needs_pass={doc.needs_pass}"
        page = doc[0]
        text = page.get_text().strip()
        result["fitz_text_sample"] = repr(text[:100])
        doc.close()
    except Exception as e:
        result["fitz"] = f"FEHLER: {traceback.format_exc()}"

    # Test 2: OCR
    try:
        import fitz
        doc = fitz.open(path)
        page = doc[0]
        tp = page.get_textpage_ocr(flags=0, language="deu+eng", dpi=72, full=True)
        text = page.get_text(textpage=tp).strip()
        result["ocr"] = f"ok — {len(text)} Zeichen"
        result["ocr_sample"] = repr(text[:100])
        doc.close()
    except Exception as e:
        result["ocr"] = f"FEHLER: {str(e)}"

    # Test 3: pypdf
    try:
        import pypdf
        reader = pypdf.PdfReader(path)
        text = reader.pages[0].extract_text() or ""
        result["pypdf"] = f"ok — {len(text)} Zeichen"
        result["pypdf_sample"] = repr(text[:100])
    except Exception as e:
        result["pypdf"] = f"FEHLER: {str(e)}"

    return JSONResponse(result)


@router.get("/debug/db-quality")
async def db_quality():
    """Umfassende DB-Qualitätsanalyse für KI-Suche-Diagnose."""
    conn = connection.get_connection()
    try:
        r = {}

        # 1. Extraction-Status Übersicht
        r["status"] = {
            row["extraction_status"]: row["cnt"]
            for row in conn.execute(
                "SELECT extraction_status, COUNT(*) AS cnt FROM documents GROUP BY extraction_status"
            ).fetchall()
        }

        # 2. Fehler nach Dateitype
        r["errors_by_ext"] = {
            row["extension"]: row["cnt"]
            for row in conn.execute(
                "SELECT extension, COUNT(*) AS cnt FROM documents "
                "WHERE extraction_status='error' GROUP BY extension ORDER BY cnt DESC"
            ).fetchall()
        }

        # 3. Chunks ohne Embedding
        no_emb = conn.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE embedding IS NULL"
        ).fetchone()[0]
        total_chunks = conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
        r["chunks"] = {"total": total_chunks, "missing_embedding": no_emb}

        # 4. Dokumente ok aber keine Chunks — nach Extension aufschlüsseln
        r["ok_without_chunks"] = conn.execute("""
            SELECT COUNT(*) FROM documents d
            WHERE d.extraction_status = 'ok'
              AND NOT EXISTS (SELECT 1 FROM document_chunks dc WHERE dc.document_id = d.id)
        """).fetchone()[0]

        r["ok_without_chunks_by_ext"] = {
            row["extension"]: row["cnt"]
            for row in conn.execute("""
                SELECT d.extension, COUNT(*) AS cnt FROM documents d
                WHERE d.extraction_status = 'ok'
                  AND NOT EXISTS (SELECT 1 FROM document_chunks dc WHERE dc.document_id = d.id)
                GROUP BY d.extension ORDER BY cnt DESC
            """).fetchall()
        }

        r["ok_without_chunks_sample"] = [
            {"filename": row["filename"], "ext": row["extension"], "size_kb": round(row["filesize"] / 1024)}
            for row in conn.execute("""
                SELECT d.filename, d.extension, d.filesize FROM documents d
                WHERE d.extraction_status = 'ok'
                  AND NOT EXISTS (SELECT 1 FROM document_chunks dc WHERE dc.document_id = d.id)
                ORDER BY d.filesize DESC LIMIT 20
            """).fetchall()
        ]

        # 5. Verbleibende Mojibake-Dokumente (nach dem Fix)
        r["remaining_mojibake"] = conn.execute("""
            SELECT COUNT(DISTINCT dc.document_id)
            FROM document_chunks dc
            JOIN documents d ON d.id = dc.document_id
            WHERE d.extraction_status = 'ok' AND dc.chunk_index < 5
            GROUP BY dc.document_id
            HAVING SUM(LENGTH(dc.content) - LENGTH(REPLACE(dc.content, CHAR(65533), ''))) * 1.0
                   / MAX(SUM(LENGTH(dc.content)), 1) > 0.30
        """).fetchone()
        r["remaining_mojibake"] = r["remaining_mojibake"][0] if r["remaining_mojibake"] else 0

        # 6. Sehr kurze Chunks (< 80 Zeichen — zu kurz für sinnvolle Suche)
        r["very_short_chunks"] = conn.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE LENGTH(content) < 80"
        ).fetchone()[0]

        # 7. Chunk-Längen-Statistik
        stats = conn.execute(
            "SELECT AVG(LENGTH(content)), MIN(LENGTH(content)), MAX(LENGTH(content)) "
            "FROM document_chunks"
        ).fetchone()
        r["chunk_length"] = {
            "avg": round(stats[0] or 0),
            "min": stats[1] or 0,
            "max": stats[2] or 0,
        }

        # 8. Dateitypen der erfolgreichen Docs
        r["ok_by_ext"] = {
            row["extension"]: row["cnt"]
            for row in conn.execute(
                "SELECT extension, COUNT(*) AS cnt FROM documents "
                "WHERE extraction_status='ok' GROUP BY extension ORDER BY cnt DESC LIMIT 10"
            ).fetchall()
        }

        # 9. Top-10 Fehlerdateien (Dateiname + Fehlerinfos fehlen, aber Namen nützlich)
        r["error_files"] = [
            {"filename": row["filename"], "ext": row["extension"]}
            for row in conn.execute(
                "SELECT filename, extension FROM documents "
                "WHERE extraction_status='error' ORDER BY filename LIMIT 20"
            ).fetchall()
        ]

        # 10. Dokumente mit pending-Status (noch nicht verarbeitet)
        r["pending"] = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE extraction_status='pending'"
        ).fetchone()[0]

        return JSONResponse(r)
    finally:
        conn.close()


@router.post("/fix/garbage-docs")
async def fix_garbage_docs():
    """Findet Dokumente mit unlesbaren Font-Encoding (Mojibake/Zeichensalat) und setzt sie
    auf 'pending' zurück. Danach werden sie beim nächsten Scan mit OCR neu extrahiert.

    Garbage-Erkennung via SQL: CHAR(65533) = U+FFFD (Replacement Character).
    Nur erste 5 Chunks pro Dokument werden geprüft (effizient, kein Memory-Problem).
    """
    def _find_and_reset():
        conn = connection.get_connection()
        try:
            # Garbage-Docs via SQL finden — CHAR(65533) = U+FFFD
            # Pro Dokument: Anteil Replacement-Characters in den ersten 5 Chunks
            garbage_ids = [
                r[0] for r in conn.execute("""
                    SELECT dc.document_id
                    FROM document_chunks dc
                    JOIN documents d ON d.id = dc.document_id
                    WHERE d.extraction_status = 'ok'
                      AND dc.chunk_index < 5
                    GROUP BY dc.document_id
                    HAVING
                        SUM(LENGTH(dc.content) - LENGTH(REPLACE(dc.content, CHAR(65533), ''))) * 1.0
                        / MAX(SUM(LENGTH(dc.content)), 1) > 0.30
                """).fetchall()
            ]

            if not garbage_ids:
                return 0, []

            placeholders = ",".join("?" * len(garbage_ids))

            filenames = [
                r[0] for r in conn.execute(
                    f"SELECT filename FROM documents WHERE id IN ({placeholders})",
                    garbage_ids
                ).fetchall()
            ]

            # Triggers deaktivieren damit DELETE schnell ist
            conn.executescript("DROP TRIGGER IF EXISTS chunks_fts_delete;"
                               "DROP TRIGGER IF EXISTS chunks_fts_insert;"
                               "DROP TRIGGER IF EXISTS chunks_fts_update;")

            with conn:
                conn.execute(
                    f"DELETE FROM document_chunks WHERE document_id IN ({placeholders})",
                    garbage_ids
                )
                conn.execute(
                    f"DELETE FROM document_content WHERE document_id IN ({placeholders})",
                    garbage_ids
                )
                try:
                    conn.execute("DELETE FROM chunks_fts")
                except Exception:
                    pass
                conn.execute(
                    f"UPDATE documents SET extraction_status='pending'"
                    f" WHERE id IN ({placeholders})",
                    garbage_ids
                )

            # FTS-Triggers neu anlegen
            conn.executescript("""
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
            return len(garbage_ids), filenames
        finally:
            conn.close()

    import asyncio
    import traceback
    try:
        count, filenames = await asyncio.get_event_loop().run_in_executor(None, _find_and_reset)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": traceback.format_exc()}, status_code=500)

    if count == 0:
        return JSONResponse({"ok": True, "count": 0,
                             "message": "Keine Dokumente mit Zeichensalat gefunden."})

    return JSONResponse({
        "ok": True,
        "count": count,
        "files": filenames,
        "message": (f"{count} Dokument(e) zurückgesetzt. "
                    f"Bitte jetzt 'Jetzt scannen' starten — sie werden mit OCR neu extrahiert.")
    })


@router.get("/ai/status")
async def ai_status():
    from scanner.embedder import ai_status as _status
    return JSONResponse(_status())


_backfill_state: dict = {"running": False, "done": 0, "total": 0, "error": ""}


@router.post("/ai/backfill")
async def ai_backfill():
    """Berechnet fehlende Embeddings für alle vorhandenen Chunks im Hintergrund."""
    if _backfill_state.get("running"):
        return JSONResponse({"ok": False, "message": "Läuft bereits"})

    def _run():
        from scanner.embedder import is_ollama_running, embed_document_chunks
        _backfill_state.update({"running": True, "done": 0, "total": 0, "error": ""})
        conn = connection.get_connection()
        try:
            if not is_ollama_running():
                _backfill_state["error"] = "Ollama nicht erreichbar"
                return
            doc_ids = conn.execute("""
                SELECT DISTINCT document_id FROM document_chunks
                WHERE embedding IS NULL
            """).fetchall()
            _backfill_state["total"] = len(doc_ids)
            for row in doc_ids:
                embed_document_chunks(conn, row["document_id"])
                _backfill_state["done"] += 1
        except Exception as e:
            _backfill_state["error"] = str(e)
        finally:
            _backfill_state["running"] = False
            conn.close()

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True, "message": "Backfill gestartet"})


@router.get("/ai/backfill/status")
async def ai_backfill_status():
    return JSONResponse(_backfill_state)


_filter_lang_state: dict = {"running": False, "done": 0, "total": 0, "deleted": 0, "error": ""}


@router.post("/ai/filter-german")
async def ai_filter_german():
    """Löscht nicht-deutsche Chunks (FR/IT) und Müll-Chunks (nur Punkte/Zahlen)
    aus Dokumenten die noch fehlende Embeddings haben."""
    if _filter_lang_state.get("running"):
        return JSONResponse({"ok": False, "message": "Läuft bereits"})

    def _is_german(text: str) -> bool:
        """Heuristik: True wenn Text deutsch ist (kein externes Paket nötig)."""
        clean = text.strip()
        if not clean or len(clean) < 30:
            return False
        # Müll-Chunks: überwiegend Punkte/Striche (Inhaltsverzeichnis-Linien)
        meaningful = clean.replace(".", "").replace("-", "").replace(" ", "").replace("\n", "")
        if len(meaningful) < len(clean) * 0.15:
            return False
        t = clean.lower()
        # Deutsche Sonderzeichen
        de_chars = sum(t.count(c) for c in "äöüßÄÖÜ")
        # Französische/Italienische Sonderzeichen
        fr_it_chars = sum(t.count(c) for c in "àâèéêëîïôùûçœæìò")
        # Wenn deutlich mehr FR/IT-Zeichen als DE-Zeichen → nicht deutsch
        if fr_it_chars > de_chars + 3:
            return False
        # Häufige deutsche Wörter
        de_words = {"der", "die", "das", "und", "für", "von", "mit", "bei",
                    "auf", "als", "ist", "sind", "wird", "werden", "nicht",
                    "des", "dem", "den", "einer", "eine", "einem", "auch",
                    "nach", "zum", "zur", "oder", "wenn", "durch", "diese"}
        # Häufige französische Wörter
        fr_words = {"les", "des", "est", "pas", "une", "qui", "que", "sur",
                    "par", "dans", "sont", "avec", "pour", "cette", "ces"}
        # Häufige italienische Wörter
        it_words = {"della", "delle", "degli", "del", "nel", "nella", "negli",
                    "per", "che", "con", "una", "uno", "sono", "essere"}
        words = set(t.split())
        de_score = len(words & de_words)
        fr_score = len(words & fr_words)
        it_score = len(words & it_words)
        # Deutsch wenn mehr DE-Treffer als FR+IT zusammen, oder DE-Sonderzeichen vorhanden
        if de_score == 0 and fr_score == 0 and it_score == 0:
            return True  # Neutral (Zahlen, Fachbegriffe) → behalten
        return de_score >= (fr_score + it_score)

    def _run():
        _filter_lang_state.update({"running": True, "done": 0, "total": 0, "deleted": 0, "error": ""})
        try:
            conn = connection.get_connection()
            conn.execute("PRAGMA busy_timeout = 15000")
            # Nur Chunks aus Dokumenten mit fehlenden Embeddings
            rows = conn.execute("""
                SELECT dc.id, dc.document_id, dc.content
                FROM document_chunks dc
                WHERE dc.embedding IS NULL
                AND dc.content IS NOT NULL
            """).fetchall()
            conn.close()

            _filter_lang_state["total"] = len(rows)
            to_delete = []

            for row in rows:
                if not _is_german(row["content"] or ""):
                    to_delete.append(row["id"])
                _filter_lang_state["done"] += 1

            if to_delete:
                conn = connection.get_connection()
                conn.execute("PRAGMA busy_timeout = 30000")
                batch = 500
                for i in range(0, len(to_delete), batch):
                    ids = to_delete[i:i+batch]
                    ph  = ",".join("?" * len(ids))
                    with conn:
                        conn.execute(f"DELETE FROM document_chunks WHERE id IN ({ph})", ids)
                conn.close()

            _filter_lang_state["deleted"] = len(to_delete)
        except Exception as e:
            _filter_lang_state["error"] = str(e)
        finally:
            _filter_lang_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True, "message": "Sprachfilter gestartet"})


@router.get("/ai/filter-german/status")
async def ai_filter_german_status():
    return JSONResponse(_filter_lang_state)


@router.post("/ai/reset-oversized")
async def ai_reset_oversized():
    """Löscht Chunks von Nicht-PDF Docs die einen einzigen zu-grossen Chunk haben
    und setzt extraction_status auf 'pending' damit der Scanner sie neu verarbeitet."""
    def _run():
        try:
            conn = connection.get_connection()
            conn.execute("PRAGMA busy_timeout = 30000")
            # Alle betroffenen document_ids finden
            rows = conn.execute("""
                SELECT dc.document_id
                FROM document_chunks dc
                JOIN documents d ON d.id = dc.document_id
                WHERE d.extension NOT IN ('.pdf', '')
                AND d.source_type = 'filesystem'
                AND length(dc.content) > 1000
                GROUP BY dc.document_id
                HAVING COUNT(dc.id) = 1
            """).fetchall()
            doc_ids = [r["document_id"] for r in rows]
            if not doc_ids:
                conn.close()
                return 0
            placeholders = ",".join("?" * len(doc_ids))
            with conn:
                conn.execute(f"DELETE FROM document_chunks WHERE document_id IN ({placeholders})", doc_ids)
                conn.execute(f"UPDATE documents SET extraction_status='pending' WHERE id IN ({placeholders})", doc_ids)
            conn.close()
            return len(doc_ids)
        except Exception as e:
            return str(e)

    import asyncio
    result = await asyncio.get_event_loop().run_in_executor(None, _run)
    if isinstance(result, str):
        return JSONResponse({"ok": False, "error": result})
    return JSONResponse({"ok": True, "reset": result,
                         "message": f"{result} Dokumente zurückgesetzt — bitte jetzt Scan starten"})


_rechunk_state: dict = {
    "running": False, "done": 0, "total": 0,
    "fixed": 0, "skipped": 0, "error": "",
    "failed_docs": [],  # [{id, filename, reason}]
}

DOC_TIMEOUT = 60  # Sekunden pro Dokument


@router.post("/ai/rechunk")
async def ai_rechunk():
    """Re-chunked Nicht-PDF-Dokumente die einen einzigen zu-grossen Chunk haben."""
    if _rechunk_state.get("running"):
        return JSONResponse({"ok": False, "message": "Läuft bereits"})

    def _rechunk_one(doc_id: int, parts: list[str]) -> str | None:
        """Führt DELETE+INSERT in eigener Connection aus. Gibt Fehlermeldung zurück oder None."""
        try:
            c = connection.get_connection()
            c.execute("PRAGMA busy_timeout = 5000")
            with c:
                c.execute("DELETE FROM document_chunks WHERE document_id = ?", (doc_id,))
                c.executemany(
                    "INSERT INTO document_chunks (document_id, chunk_index, page_number, content) VALUES (?,?,?,?)",
                    [(doc_id, i, None, part) for i, part in enumerate(parts)]
                )
            c.close()
            return None
        except Exception as e:
            return str(e)

    def _run():
        from scanner.extractors import split_text_into_chunks
        _rechunk_state.update({
            "running": True, "done": 0, "total": 0,
            "fixed": 0, "skipped": 0, "error": "", "failed_docs": [],
        })

        # Kandidaten mit einer einzigen Connection finden
        try:
            conn = connection.get_connection()
            conn.execute("PRAGMA busy_timeout = 30000")
            # Direkte JOIN-Query — funktioniert schnell sobald Migration 005
            # den Index idx_document_chunks_doc erstellt hat
            rows = conn.execute("""
                SELECT d.id AS document_id, d.filename, dc.content
                FROM documents d
                JOIN document_chunks dc ON dc.document_id = d.id
                WHERE d.extraction_status = 'ok'
                AND d.extension NOT IN ('.pdf', '')
                AND d.source_type = 'filesystem'
                AND length(dc.content) > 1000
                GROUP BY d.id
                HAVING COUNT(dc.id) = 1
            """).fetchall()
            conn.close()
        except Exception as e:
            _rechunk_state.update({"running": False, "error": str(e)})
            return

        candidates = [
            {"document_id": r["document_id"], "filename": r["filename"], "content": r["content"]}
            for r in rows
        ]

        _rechunk_state["total"] = len(candidates)

        try:
            for row in candidates:
                doc_id   = row["document_id"]
                filename = row["filename"]
                content  = row.get("content") or ""
                try:
                    parts = split_text_into_chunks(content)
                except Exception as e:
                    _rechunk_state["done"]    += 1
                    _rechunk_state["skipped"] += 1
                    _rechunk_state["failed_docs"].append(
                        {"id": doc_id, "filename": filename, "reason": f"Split-Fehler: {e}"}
                    )
                    continue

                _rechunk_state["done"] += 1

                if len(parts) <= 1:
                    continue

                # Mit Timeout ausführen
                result_holder: list = []
                def _do(doc_id=doc_id, parts=parts, holder=result_holder):
                    holder.append(_rechunk_one(doc_id, parts))

                t = threading.Thread(target=_do, daemon=True)
                t.start()
                t.join(timeout=DOC_TIMEOUT)

                if t.is_alive():
                    log.warning("Rechunk timeout doc %s (%s)", doc_id, filename)
                    _rechunk_state["skipped"] += 1
                    _rechunk_state["failed_docs"].append(
                        {"id": doc_id, "filename": filename, "reason": f"Timeout nach {DOC_TIMEOUT}s"}
                    )
                elif result_holder and result_holder[0] is not None:
                    log.warning("Rechunk doc %s (%s): %s", doc_id, filename, result_holder[0])
                    _rechunk_state["skipped"] += 1
                    _rechunk_state["failed_docs"].append(
                        {"id": doc_id, "filename": filename, "reason": result_holder[0]}
                    )
                else:
                    _rechunk_state["fixed"] += 1
        except Exception as e:
            _rechunk_state["error"] = str(e)
        finally:
            _rechunk_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True, "message": "Re-chunk gestartet"})


@router.get("/ai/rechunk/status")
async def ai_rechunk_status():
    return JSONResponse(_rechunk_state)


@router.get("/ai/embed-test")
async def ai_embed_test():
    """Testet Ollama-Embedding — versucht 3 Chunks aus failing docs, Batch-1 und Batch-20."""
    import asyncio, httpx

    def _run():
        conn = connection.get_connection()
        try:
            # 3 chunks aus Dokumenten die komplett ohne Embedding sind
            rows = conn.execute("""
                SELECT dc.id, dc.content, d.filename, length(dc.content) as clen
                FROM document_chunks dc
                JOIN documents d ON d.id = dc.document_id
                WHERE dc.embedding IS NULL
                ORDER BY clen DESC
                LIMIT 3
            """).fetchall()
            if not rows:
                return {"status": "Alle Chunks haben Embeddings — fertig!"}

            results = []
            for row in rows:
                text = row["content"] or ""
                # Test 1: Einzelner Chunk
                try:
                    resp = httpx.post("http://localhost:11434/api/embed",
                        json={"model": "nomic-embed-text", "input": [text]},
                        timeout=60)
                    data = resp.json()
                    vecs = data.get("embeddings", [])
                    results.append({
                        "filename": row["filename"],
                        "chunk_id": row["id"],
                        "text_len": row["clen"],
                        "http_status": resp.status_code,
                        "ok": len(vecs) > 0 and len(vecs[0]) > 0,
                        "error": data.get("error"),
                    })
                except Exception as e:
                    results.append({
                        "filename": row["filename"],
                        "chunk_id": row["id"],
                        "text_len": row["clen"],
                        "exception": str(e),
                    })
            return {"tests": results}
        finally:
            conn.close()

    result = await asyncio.get_event_loop().run_in_executor(None, _run)
    return JSONResponse(result)


_normalize_state: dict = {"running": False, "done": 0, "total": 0, "changed": 0, "error": ""}


@router.post("/ai/normalize-ligatures")
async def ai_normalize_ligatures():
    """Normalisiert Ligaturen (ﬂ→fl) + OCR-Leerzeichen in Chunk-Inhalten,
    löscht Embeddings ALLER Chunks (vollständige Neuberechnung), baut FTS neu."""
    if _normalize_state.get("running"):
        return JSONResponse({"ok": False, "message": "Läuft bereits"})

    def _run():
        from scanner.extractors import normalize_text
        _normalize_state.update({"running": True, "done": 0, "total": 0, "changed": 0, "error": ""})
        conn = connection.get_connection()
        try:
            rows = conn.execute("SELECT id, content FROM document_chunks").fetchall()
            _normalize_state["total"] = len(rows)
            changed = 0
            for row in rows:
                orig = row["content"] or ""
                norm = normalize_text(orig)
                if norm != orig:
                    conn.execute(
                        "UPDATE document_chunks SET content = ? WHERE id = ?",
                        (norm, row["id"])
                    )
                    changed += 1
                _normalize_state["done"] += 1
            conn.commit()
            _normalize_state["changed"] = changed
            # Alle Embeddings löschen → Backfill muss danach laufen
            conn.execute("UPDATE document_chunks SET embedding = NULL")
            conn.commit()
            # FTS-Index neu aufbauen
            try:
                conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
                conn.commit()
            except Exception as fts_err:
                _normalize_state["error"] = f"FTS-Rebuild: {fts_err}"
        except Exception as e:
            conn.rollback()
            _normalize_state["error"] = str(e)
        finally:
            _normalize_state["running"] = False
            conn.close()

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True, "message": "Normalisierung gestartet — danach Embeddings neu generieren!"})


@router.get("/ai/normalize-ligatures/status")
async def ai_normalize_status():
    return JSONResponse(_normalize_state)


@router.get("/ai/diagnostics")
async def ai_diagnostics():
    """Gibt den Embedding-Zustand als JSON zurück (läuft im Thread-Pool)."""
    import asyncio
    loop = asyncio.get_event_loop()

    def _query():
        conn = connection.get_connection()
        try:
            total_chunks = conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
            with_emb     = conn.execute(
                "SELECT COUNT(*) FROM document_chunks WHERE embedding IS NOT NULL"
            ).fetchone()[0]
            oversized = conn.execute("""
                SELECT COUNT(*) FROM (
                    SELECT dc.document_id
                    FROM document_chunks dc
                    JOIN documents d ON d.id = dc.document_id
                    WHERE d.extension NOT IN ('.pdf')
                    AND length(dc.content) > 1000
                    GROUP BY dc.document_id
                    HAVING COUNT(*) = 1
                )
            """).fetchone()[0]
            # Chunks ohne Embedding nach Grösse aufschlüsseln
            missing_by_size = {}
            for label, lo, hi in [("≤500", 0, 500), ("501-2000", 500, 2000),
                                   ("2001-5000", 2000, 5000), (">5000", 5000, 10**9)]:
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM document_chunks WHERE embedding IS NULL "
                    "AND length(content) > ? AND length(content) <= ?", (lo, hi)
                ).fetchone()[0]
                missing_by_size[label] = cnt
            return {
                "total_chunks":            total_chunks,
                "with_embedding":          with_emb,
                "missing_embedding":       total_chunks - with_emb,
                "missing_by_size":         missing_by_size,
                "oversized_single_chunks": oversized,
                "embedding_coverage_pct":  round(with_emb / total_chunks * 100, 1) if total_chunks else 0,
            }
        finally:
            conn.close()

    data = await loop.run_in_executor(None, _query)
    return JSONResponse(data)


_ollama_install_state: dict = {"running": False, "done": False, "error": "", "log": []}


@router.post("/ai/install-ollama")
async def install_ollama():
    if _ollama_install_state.get("running"):
        return JSONResponse({"ok": False, "message": "Installation läuft bereits"})

    def _run():
        _ollama_install_state.update({"running": True, "done": False, "error": "", "log": []})
        try:
            proc = subprocess.Popen(
                "curl -fsSL https://ollama.com/install.sh | sh",
                shell=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            for line in proc.stdout:
                _ollama_install_state["log"].append(line.rstrip())
            proc.wait()
            from scanner.embedder import is_ollama_installed
            if is_ollama_installed():
                _ollama_install_state.update({"running": False, "done": True})
            else:
                # Der curl-Installer beendet sich bevor der macOS-Passwortdialog
                # erscheint — die Installation läuft danach im Hintergrund weiter.
                _ollama_install_state.update({"running": False, "needs_reload": True})
        except Exception as e:
            _ollama_install_state.update({"running": False, "error": str(e)})

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True})


@router.get("/ai/install-ollama/status")
async def install_ollama_status():
    return JSONResponse(_ollama_install_state)


_GITHUB_REPO = "inderfab/archivio"


@router.get("/update/check")
async def update_check():
    """Prüft ob eine neue Version auf GitHub verfügbar ist."""
    import requests as _req
    current = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "0.0.0"
    try:
        resp = _req.get(
            f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest",
            timeout=10,
            headers={"Accept": "application/vnd.github+json"},
        )
        if resp.status_code != 200:
            return JSONResponse({"current": current, "update_available": False})
        remote_ver = resp.json().get("tag_name", "").lstrip("v")
        update_available = bool(remote_ver) and remote_ver != current
        return JSONResponse({
            "current": current,
            "latest": remote_ver,
            "update_available": update_available,
            "download_url": "https://inderfab.github.io/archivio/index.html",
        })
    except Exception:
        return JSONResponse({"current": current, "update_available": False})


@router.get("/debug/chunks")
async def debug_chunks(filename: str = "", q: str = ""):
    """Zeigt Chunk-Inhalte für ein Dokument (filename) oder Texte die q enthalten."""
    import asyncio
    loop = asyncio.get_event_loop()
    def _query():
        conn = connection.get_connection()
        try:
            if filename:
                rows = conn.execute("""
                    SELECT dc.id, dc.chunk_index, dc.page_number,
                           length(dc.content) AS len,
                           substr(dc.content, 1, 300) AS preview,
                           dc.embedding IS NOT NULL AS has_emb,
                           d.filename
                    FROM document_chunks dc
                    JOIN documents d ON d.id = dc.document_id
                    WHERE d.filename LIKE ?
                    ORDER BY dc.chunk_index
                    LIMIT 30
                """, (f"%{filename}%",)).fetchall()
            elif q:
                rows = conn.execute("""
                    SELECT dc.id, dc.chunk_index, dc.page_number,
                           length(dc.content) AS len,
                           substr(dc.content, 1, 300) AS preview,
                           dc.embedding IS NOT NULL AS has_emb,
                           d.filename
                    FROM document_chunks dc
                    JOIN documents d ON d.id = dc.document_id
                    WHERE LOWER(dc.content) LIKE ?
                    LIMIT 20
                """, (f"%{q.lower()}%",)).fetchall()
            else:
                return {"error": "filename oder q Parameter erforderlich"}
            return {"count": len(rows), "chunks": [dict(r) for r in rows]}
        finally:
            conn.close()
    data = await loop.run_in_executor(None, _query)
    return JSONResponse(data)

@router.get("/debug/dist")
async def debug_dist():
    from web.dashboard import _DIST
    files = list(_DIST.glob("*.zip")) if _DIST.exists() else []
    return {"dist_path": str(_DIST), "exists": _DIST.exists(), "files": [f.name for f in files]}


# ── Recall-Tests ──────────────────────────────────────────────────────────────

@router.get("/test/recall")
async def test_recall(n: int = 10, seed: int = 42):
    """
    Recall-Test: Zufällige Chunks aus der DB → Suche mit Textausschnitt → Dokument gefunden?
    Läuft direkt auf dem Server wo die DB liegt.
    ?n=10   Anzahl Chunks (default 10)
    ?seed=  Zufalls-Seed für Reproduzierbarkeit
    """
    import asyncio, random, time
    loop = asyncio.get_event_loop()

    def _run():
        import random as _rnd
        from scanner.embedder import keyword_search_chunks, embed_query, vector_search, is_ollama_running

        _rnd.seed(seed)
        conn = connection.get_connection()
        results = {"keyword": [], "vector": [], "summary": {}}

        try:
            # Mehr Kandidaten holen, dann Python-seitig filtern
            candidates = conn.execute("""
                SELECT dc.id, dc.document_id, dc.content, dc.chunk_index,
                       dc.page_number, d.filename, d.extension
                FROM document_chunks dc
                JOIN documents d ON d.id = dc.document_id
                WHERE dc.embedding IS NOT NULL
                  AND length(dc.content) >= 200
                  AND d.extraction_status = 'ok'
                  AND d.extension IN ('.pdf', '.docx', '.txt', '.doc')
                ORDER BY RANDOM()
                LIMIT ?
            """, (n * 5,)).fetchall()

            # Mojibake und OCR-Müll filtern: mind. 55% echte Buchstaben im Content
            import re as _re2
            def _is_clean(row):
                text = row["content"] or ""
                if not text:
                    return False
                alpha = sum(c.isalpha() for c in text)
                if alpha / len(text) < 0.55:
                    return False
                # Keine mehrheitlich französischen/italienischen Zeilen
                fr_it_hits = len(_re2.findall(
                    r'\b(le|la|les|de|du|des|une|sur|est|pour|'
                    r'della|delle|degli|con|per|nel|sul|che)\b',
                    text.lower()))
                if fr_it_hits > 5:
                    return False
                return True

            rows = [r for r in candidates if _is_clean(r)][:n]
            if not rows:
                return {"error": "Keine geeigneten Chunks in der DB."}

            def _excerpt(text, length=100):
                """Wählt den besten Satz aus dem Text: deutsch, keine Zahlen-Zeilen."""
                import re as _re
                lines = [l.strip() for l in text.split('\n') if len(l.strip()) >= 40]
                # Qualitätskriterien: mehrheitlich Buchstaben, kein reines OCR-Rauschen
                def _quality(line):
                    alpha = sum(c.isalpha() for c in line)
                    if alpha / max(len(line), 1) < 0.5:   # < 50% Buchstaben → Zahlen/Symbole
                        return 0
                    # Französisch/Italienisch erkennen (häufige Funktionswörter)
                    fr_it = len(_re.findall(r'\b(le|la|les|de|du|des|au|aux|une|sur|est|pour|'
                                           r'il|che|della|delle|degli|con|per|nel|sul)\b',
                                           line.lower()))
                    if fr_it >= 2:                         # mehrere FR/IT Wörter → überspringen
                        return 0
                    return alpha  # höher = besser
                scored = [(l, _quality(l)) for l in lines]
                good = [(l, s) for l, s in scored if s > 0]
                if not good:
                    # Fallback: einfach Mitte nehmen
                    text = text.strip()
                    mid = len(text) // 2
                    return text[max(0, mid - length // 2):mid + length // 2].strip()
                # Besten Satz aus der Mitte des Textes bevorzugen
                mid_idx = len(good) // 2
                best_line = good[mid_idx][0]
                return best_line[:length].strip()

            # ── Keyword-Recall ────────────────────────────────────────────────
            kw_hits = 0
            for row in rows:
                query   = _excerpt(row["content"], length=70)
                hits    = keyword_search_chunks(conn, query, limit=10)
                found   = any(h["document_id"] == row["document_id"] for h in hits)
                kw_hits += int(found)
                results["keyword"].append({
                    "filename":  row["filename"],
                    "page":      row["page_number"],
                    "query":     query[:60] + ("…" if len(query) > 60 else ""),
                    "found":     found,
                    "top3":      [h["filename"] for h in hits[:3]],
                })

            # ── Vektor-Recall ─────────────────────────────────────────────────
            vec_hits      = 0
            ollama_running = is_ollama_running()
            if ollama_running:
                for row in rows:
                    query  = _excerpt(row["content"], length=200)
                    t0     = time.time()
                    qvec   = embed_query(query)
                    hits   = vector_search(conn, qvec, limit=10) if qvec is not None else []
                    found  = any(h["document_id"] == row["document_id"] for h in hits)
                    vec_hits += int(found)
                    results["vector"].append({
                        "filename":    row["filename"],
                        "page":        row["page_number"],
                        "found":       found,
                        "embed_ms":    round((time.time() - t0) * 1000),
                        "top3":        [h["filename"] for h in hits[:3]],
                        "top3_scores": [round(h.get("score", 0), 3) for h in hits[:3]],
                    })

            # ── Summary ───────────────────────────────────────────────────────
            total = len(rows)
            results["summary"] = {
                "total_chunks_tested": total,
                "keyword_hits":        kw_hits,
                "keyword_recall_pct":  round(kw_hits / total * 100, 1),
                "keyword_pass":        kw_hits >= total * 0.7,
                "vector_tested":       ollama_running,
                "vector_hits":         vec_hits if ollama_running else None,
                "vector_recall_pct":   round(vec_hits / total * 100, 1) if ollama_running else None,
                "vector_pass":         (vec_hits >= total * 0.7) if ollama_running else None,
                "overall_pass":        kw_hits >= total * 0.7 and (not ollama_running or vec_hits >= total * 0.7),
            }

        finally:
            conn.close()

        return results

    data = await loop.run_in_executor(None, _run)
    return JSONResponse(data)


@router.get("/test/recall/multi")
async def test_recall_multi(n: int = 10, runs: int = 5):
    """Mehrere Recall-Durchläufe mit verschiedenen Seeds → statistisch belastbares Ergebnis."""
    import asyncio
    loop = asyncio.get_event_loop()

    async def _one_run(seed):
        return await loop.run_in_executor(None, lambda: None)  # placeholder

    # Sequenziell laufen lassen (DB-Last verteilen)
    import httpx
    base = "http://localhost:8000"
    results = []
    for seed in range(runs):
        try:
            resp = httpx.get(f"{base}/api/test/recall?n={n}&seed={seed*17+42}", timeout=300)
            if resp.status_code == 200:
                results.append(resp.json()["summary"])
        except Exception:
            pass

    if not results:
        return {"error": "Keine Ergebnisse"}

    avg_kw  = round(sum(r["keyword_recall_pct"] for r in results) / len(results), 1)
    avg_vec = round(sum(r["vector_recall_pct"] for r in results if r.get("vector_recall_pct") is not None) / len(results), 1)
    hybrids = []
    for r in results:
        kw = r.get("keyword_hits", 0)
        vec = r.get("vector_hits", 0) or 0
        total = r.get("total_chunks_tested", 1)
        # Hybrid nicht direkt verfügbar, approximieren
        hybrids.append(round(min(kw + vec * 0.5, total) / total * 100, 1))

    return {
        "runs": len(results),
        "n_per_run": n,
        "avg_keyword_recall_pct": avg_kw,
        "avg_vector_recall_pct": avg_vec,
        "keyword_pass": avg_kw >= 70,
        "vector_pass": avg_vec >= 70,
        "per_run": results,
    }


@router.get("/debug/mail")
async def debug_mail():
    """Mail-Konfiguration und Status für Diagnose."""
    conn = connection.get_connection()
    try:
        # mail_scan_config Status
        configs = [dict(r) for r in conn.execute("""
            SELECT msc.*, p.name AS project_name
            FROM mail_scan_config msc
            LEFT JOIN projects p ON p.id = msc.project_id
        """).fetchall()]

        # Wie viele Email-Dokumente existieren
        email_count = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE source_type='email'"
        ).fetchone()[0]

        # Mail-Einstellungen
        mail_cfg = {
            "host":     settings.get("mail.host", ""),
            "port":     settings.get("mail.port", 993),
            "username": settings.get("mail.username", ""),
            "has_password": bool(settings.get("mail.password", "")),
        }

        return JSONResponse({
            "mail_config": mail_cfg,
            "mailboxes": configs,
            "email_docs_in_db": email_count,
        })
    finally:
        conn.close()


@router.post("/debug/mail/scan-test")
async def debug_mail_scan_test(mailbox: str = ""):
    """Testet den Mail-Scan für ein Postfach und gibt Details zurück."""
    import traceback
    try:
        from scanner.mail_scanner import connect_imap, _fetch_uids, _fetch_header, build_email_record, mail_exists

        conn = connection.get_connection()
        row = conn.execute(
            "SELECT * FROM mail_scan_config WHERE mailbox_name LIKE ? LIMIT 1",
            (f"%{mailbox}%",)
        ).fetchone() if mailbox else conn.execute(
            "SELECT * FROM mail_scan_config WHERE active=1 AND project_id IS NOT NULL LIMIT 1"
        ).fetchone()
        conn.close()

        if not row:
            return JSONResponse({"error": f"Kein aktives Postfach gefunden für: '{mailbox}'"})

        mb_name    = row["mailbox_name"]
        project_id = row["project_id"]

        client = connect_imap()
        uids   = _fetch_uids(client, mb_name)

        results = {"mailbox": mb_name, "project_id": project_id,
                   "total_uids": len(uids), "sample": []}

        conn = connection.get_connection()
        for uid in uids[:5]:
            try:
                hdr = _fetch_header(client, uid)
                mid = (hdr.get("Message-ID") or "").strip()
                exists = mail_exists(conn, mid) if mid else False
                results["sample"].append({
                    "uid":        uid.decode(),
                    "message_id": mid[:60] if mid else "(fehlt)",
                    "subject":    str(hdr.get("Subject", ""))[:60],
                    "already_in_db": exists,
                })
            except Exception as e:
                results["sample"].append({"uid": str(uid), "error": str(e)})
        conn.close()
        client.logout()
        return JSONResponse(results)

    except Exception:
        return JSONResponse({"error": traceback.format_exc()}, status_code=500)


@router.post("/mail/reset-and-rescan")
async def mail_reset_and_rescan():
    """Löscht alle E-Mail-Dokumente und startet Mail-Scan neu.
    Der neue Scan speichert Mails inkl. Chunks + Embeddings (mit Fehlertoleranz).
    """
    # Mails aus DB löschen
    conn = connection.get_connection()
    try:
        with conn:
            conn.execute("DELETE FROM documents WHERE source_type = 'email'")
        deleted = conn.execute(
            "SELECT changes()"
        ).fetchone()[0]
    finally:
        conn.close()

    # Mail-Scan starten
    if _mail_scan.get("status") == "running":
        return JSONResponse({"ok": True, "deleted": deleted,
                             "message": "Mails gelöscht. Scan läuft bereits."})
    _mail_scan.clear()
    _mail_scan["status"]     = "running"
    _mail_scan["started_at"] = _now()
    threading.Thread(target=_run_mail_scan, daemon=True).start()

    return JSONResponse({"ok": True, "deleted": deleted,
                         "message": f"{deleted} Mails gelöscht. Scan gestartet — inkl. Chunking + Embedding."})


# ── PDF-Metadaten-Backfill ────────────────────────────────────────────────────

_meta_backfill_state: dict = {"running": False, "done": 0, "total": 0, "error": ""}


@router.post("/pdf-metadata/backfill")
async def pdf_metadata_backfill():
    """Liest Creator/Producer aus allen gescannten PDFs und setzt is_plan/creator_app."""
    if _meta_backfill_state.get("running"):
        return JSONResponse({"ok": False, "message": "Läuft bereits"})

    def _run():
        from scanner import extractors
        from db import queries
        _meta_backfill_state.update({"running": True, "done": 0, "total": 0, "error": ""})
        conn = connection.get_connection()
        try:
            rows = conn.execute("""
                SELECT d.id, dp.path
                FROM documents d
                JOIN document_paths dp ON dp.document_id = d.id
                WHERE d.extension = '.pdf' AND d.extraction_status = 'ok'
                GROUP BY d.id
            """).fetchall()
            _meta_backfill_state["total"] = len(rows)
            for row in rows:
                try:
                    meta = extractors.extract_pdf_metadata(Path(row["path"]))
                    if meta:
                        with conn:
                            queries.update_metadata(conn, row["id"], meta)
                except Exception:
                    pass
                _meta_backfill_state["done"] += 1
        except Exception as e:
            _meta_backfill_state["error"] = str(e)
        finally:
            _meta_backfill_state["running"] = False
            conn.close()

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True, "message": "Metadaten-Backfill gestartet"})


@router.get("/pdf-metadata/backfill/status")
async def pdf_metadata_backfill_status():
    return JSONResponse(_meta_backfill_state)


# ── Diagnose ──────────────────────────────────────────────────────────────────

@router.get("/debug/diagnostics", response_class=HTMLResponse)
async def diagnostics():
    import sys
    checks: list[dict] = []

    def _chk(label: str, value: str, ok: bool | None = True, detail: str = ""):
        checks.append({"label": label, "value": value, "ok": ok, "detail": detail})

    # Umgebung
    py_version = sys.version.split()[0]
    py_path    = sys.executable
    py_minor   = sys.version_info.minor
    # Herkunft und FDA-Fähigkeit bestimmen
    if "/Library/Frameworks/Python.framework/" in py_path:
        py_source = "python.org (FDA-fähig)"
        py_ok     = py_minor >= 12
        py_detail = "" if py_minor >= 12 else "Version zu alt — bitte Python 3.13 von python.org installieren"
    elif "/opt/homebrew/" in py_path or "/usr/local/Cellar/" in py_path:
        py_source = "Homebrew (kein FDA)"
        py_ok     = False
        py_detail = "Homebrew-Python hat keinen Festplattenvollzugriff — bitte Python 3.13 von python.org installieren"
    elif py_path.startswith("/usr/bin/"):
        py_source = "macOS System-Python"
        py_ok     = False
        py_detail = "System-Python hat keinen Festplattenvollzugriff — bitte Python 3.13 von python.org installieren"
    else:
        py_source = "unbekannte Herkunft"
        py_ok     = py_minor >= 12
        py_detail = "" if py_minor >= 12 else "Version zu alt"
    _chk("Python", f"{py_version}  —  {py_source}", ok=py_ok, detail=py_detail)
    _chk("Python-Pfad", py_path)
    data_dir = os.environ.get("ARCHIVIO_DATA_DIR", "")
    _chk("ARCHIVIO_DATA_DIR", data_dir or "(nicht gesetzt)", ok=bool(data_dir),
         detail="" if data_dir else "Variable fehlt — Pfade evtl. falsch")
    _chk("HOME", os.environ.get("HOME", "(nicht gesetzt)"))

    # Config
    from config import settings as _s
    cfg_path   = _s._CONFIG_PATH
    cfg_exists = cfg_path.exists()
    _chk("Config-Datei", str(cfg_path), ok=cfg_exists,
         detail="" if cfg_exists else "Datei fehlt!")
    if cfg_exists:
        base_folders = _s.get("scanner.base_folders", [])
        _chk("Base-Folders (Config)", f"{len(base_folders)} konfiguriert",
             ok=bool(base_folders),
             detail="; ".join(f.get("path","?") for f in base_folders) if base_folders else "Keine Ordner eingetragen")

    # Datenbank
    db_rel  = settings.get("database.path", "archivio.db")
    db_path = (Path(data_dir) / db_rel) if data_dir else Path(db_rel)
    db_ok   = db_path.exists()
    _chk("Datenbank", str(db_path), ok=db_ok,
         detail=f"{db_path.stat().st_size // 1024} KB" if db_ok else "Datei fehlt!")

    # /Volumes
    try:
        vols = [e.name for e in os.scandir("/Volumes") if not e.name.startswith(".")]
        _chk("/Volumes (gemountete Laufwerke)",
             ", ".join(vols) if vols else "(keine)",
             detail=f"{len(vols)} Laufwerk(e)")
    except Exception as exc:
        _chk("/Volumes", str(exc), ok=False)

    # Desktop-Zugriff (Festplattenvollzugriff-Test)
    home    = Path(os.environ.get("HOME", str(Path.home())))
    desktop = home / "Desktop"
    try:
        list(os.scandir(str(desktop)))
        _chk("Desktop-Zugriff", str(desktop))
    except PermissionError:
        _chk("Desktop-Zugriff", str(desktop), ok=False,
             detail="Kein Zugriff — Festplattenvollzugriff erteilen")
    except FileNotFoundError:
        _chk("Desktop-Zugriff", str(desktop), ok=None, detail="Ordner nicht gefunden")

    # Base-Folders Prüfung
    for f in settings.get("scanner.base_folders", []):
        lbl  = f.get("label", "?")
        path = f.get("path", "")
        if not path:
            _chk(f"Ordner [{lbl}]", "(leer)", ok=False, detail="Pfad fehlt")
            continue
        p = Path(path)
        if not p.exists():
            _chk(f"Ordner [{lbl}]", path, ok=False,
                 detail="Existiert nicht / NAS nicht gemountet")
            continue
        try:
            dirs = [e.name for e in os.scandir(path)
                    if e.is_dir() and not e.name.startswith(".")]
            _chk(f"Ordner [{lbl}]", path,
                 detail=f"{len(dirs)} Unterordner: {', '.join(dirs[:6])}")
        except PermissionError:
            _chk(f"Ordner [{lbl}]", path, ok=False,
                 detail="Kein Lesezugriff — Festplattenvollzugriff prüfen")

    # DB-Qualität
    try:
        from db import connection as _dbc
        _conn = _dbc.get_connection()
        status_counts = {r[0]: r[1] for r in _conn.execute(
            "SELECT extraction_status, COUNT(*) FROM documents GROUP BY extraction_status"
        ).fetchall()}
        total_docs = sum(status_counts.values())
        ok_docs   = status_counts.get("ok", 0)
        err_docs  = status_counts.get("error", 0)
        pend_docs = status_counts.get("pending", 0)
        unsup_docs = status_counts.get("unsupported", 0)
        doc_ok = err_docs == 0
        _chk(
            "Dokumente gesamt", str(total_docs),
            ok=True if doc_ok else None,
            detail=f"ok: {ok_docs}, Fehler: {err_docs}, ausstehend: {pend_docs}, nicht unterstützt: {unsup_docs}"
        )
        total_chunks = _conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
        missing_emb  = _conn.execute("SELECT COUNT(*) FROM document_chunks WHERE embedding IS NULL").fetchone()[0]
        docs_no_chunk = _conn.execute(
            "SELECT COUNT(*) FROM documents d WHERE extraction_status='ok' "
            "AND NOT EXISTS (SELECT 1 FROM document_chunks dc WHERE dc.document_id = d.id)"
        ).fetchone()[0]
        emb_ok = missing_emb == 0
        if not emb_ok:
            emb_detail = (
                f"{missing_emb} Chunks ohne Embedding — "
                f'<a href="#" onclick="fetch(\'/api/ai/backfill\',{{method:\'POST\'}})'
                f'.then(function(){{alert(\'Backfill gestartet — kann einige Minuten dauern.\')}})'
                f';return false">Backfill jetzt starten</a>'
            )
        elif docs_no_chunk:
            emb_detail = f"{docs_no_chunk} Dokumente ohne Chunks"
        else:
            emb_detail = ""
        _chk(
            "Embeddings",
            f"{total_chunks - missing_emb} / {total_chunks} Chunks",
            ok=emb_ok if total_chunks > 0 else None,
            detail=emb_detail
        )
        pdfs_ok       = _conn.execute(
            "SELECT COUNT(*) FROM documents WHERE extension='.pdf' AND extraction_status='ok'"
        ).fetchone()[0]
        pdfs_with_meta = _conn.execute(
            "SELECT COUNT(*) FROM documents WHERE extension='.pdf' AND extraction_status='ok'"
            " AND json_extract(metadata,'$.is_plan') IS NOT NULL"
        ).fetchone()[0]
        pdfs_missing_meta = pdfs_ok - pdfs_with_meta
        if pdfs_missing_meta > 0:
            meta_detail = (
                f"{pdfs_missing_meta} PDFs ohne Plan-Metadaten — "
                f'<a href="#" onclick="startMetaBackfill(this);return false">Jetzt nachführen</a>'
            )
            meta_ok = None
        else:
            meta_detail = ""
            meta_ok = True
        _chk(
            "Plan-Metadaten (PDFs)",
            f"{pdfs_with_meta} / {pdfs_ok} PDFs analysiert",
            ok=meta_ok if pdfs_ok > 0 else None,
            detail=meta_detail
        )
        _conn.close()
    except Exception as _exc:
        _chk("DB-Qualität", str(_exc), ok=False)

    # Ollama
    try:
        from scanner.embedder import is_ollama_installed, is_ollama_running, ai_status, EMBED_MODEL, LLM_MODEL
        if not is_ollama_installed():
            _chk("Ollama", "Nicht installiert", ok=False,
                 detail='<a href="https://bauchat.ch/docs.html#ki" target="_blank">Anleitung zur Installation</a>')
        elif not is_ollama_running():
            _chk("Ollama", "Installiert, aber nicht gestartet", ok=False,
                 detail="Ollama starten: Menubar → KI-Suche klicken, oder 'ollama serve' im Terminal")
        else:
            status = ai_status()
            if status["ok"]:
                _chk("Ollama", "Läuft", ok=True,
                     detail=f"Modelle: {EMBED_MODEL}, {LLM_MODEL}")
            else:
                _chk("Ollama", f"Läuft – {status['reason']}", ok=None,
                     detail=f"Modelle prüfen: {EMBED_MODEL}, {LLM_MODEL}")
    except Exception as _exc:
        _chk("Ollama", str(_exc), ok=False)

    # HTML rendern
    rows = []
    for c in checks:
        ok = c["ok"]
        icon  = "✓" if ok is True  else ("⚠" if ok is None else "✗")
        color = "#166534" if ok is True else ("#92400e" if ok is None else "#991b1b")
        bg    = "#f0fdf4" if ok is True else ("#fffbeb" if ok is None else "#fef2f2")
        det   = (f'<div style="font-size:11px;color:#6b7280;margin-top:2px;">{c["detail"]}</div>'
                 if c["detail"] else "")
        rows.append(
            f'<div style="display:grid;grid-template-columns:22px 1fr;gap:8px;align-items:start;'
            f'padding:8px 12px;border-bottom:1px solid var(--border);background:{bg};">'
            f'<span style="color:{color};font-weight:700;font-size:14px;">{icon}</span>'
            f'<div>'
            f'<div style="font-size:11px;font-weight:600;color:var(--text-2);text-transform:uppercase;'
            f'letter-spacing:.05em;">{c["label"]}</div>'
            f'<div style="font-size:12px;font-family:monospace;color:var(--text);word-break:break-all;">'
            f'{c["value"]}</div>{det}'
            f'</div></div>'
        )
    ts = __import__("datetime").datetime.now().strftime("%H:%M:%S")
    html = (
        f'<div style="margin-top:12px;border:1px solid var(--border);'
        f'border-radius:var(--radius);overflow:hidden;">'
        + "".join(rows) +
        f'<div style="padding:6px 12px;font-size:11px;color:var(--text-3);">'
        f'Ausgeführt um {ts}</div></div>'
    )
    return HTMLResponse(html)


def _resolve_pdf_path(conn, document_id: int) -> tuple[Path | None, str | None]:
    """Löst document_id -> Pfad auf, geprüft gegen die konfigurierten NAS-Wurzelpfade
    (dieselbe Whitelist wie list_folder/open_file im MCP-Connector und die Foto-Galerie)
    UND auf .pdf-Endung beschränkt. Nie einen Client-Pfad direkt entgegennehmen."""
    row = conn.execute(
        """
        SELECT d.extension AS extension, dp.path AS path
        FROM documents d
        JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        WHERE d.id = ?
        """,
        (document_id,),
    ).fetchone()
    if not row:
        return None, "Dokument nicht gefunden"
    if (row["extension"] or "").lower() != ".pdf":
        return None, "kein PDF"
    base_folders = settings.get("scanner.base_folders", [])
    allowed = [f.get("path") for f in base_folders if f.get("path")]
    if not allowed:
        return None, "keine NAS-Ordner konfiguriert"
    try:
        target = Path(row["path"]).resolve()
    except Exception as e:
        return None, f"ungültiger Pfad: {e}"
    ok = any(target == Path(a).resolve() or Path(a).resolve() in target.parents for a in allowed)
    if not ok:
        return None, "Pfad ausserhalb der erlaubten Archivio-Ordner"
    if not target.exists():
        return None, "Datei nicht gefunden"
    return target, None


class MergePdfBody(BaseModel):
    ids: list[int]


@router.post("/pdf-zusammenfuehren")
async def merge_pdfs(body: MergePdfBody):
    """Fügt mehrere ausgewählte PDF-Dokumente zu einem neuen PDF zusammen. Antwortet
    mit Base64 statt einem direkten Datei-Download, damit das Frontend übersprungene
    Dateien (kein PDF, nicht lesbar, ausserhalb der Whitelist) in derselben Antwort
    melden kann -- der eigentliche Speicherort wird danach ganz normal über den
    Browser-Download gewählt, kein Helper-Umbau nötig."""
    if not body.ids:
        return JSONResponse({"ok": False, "error": "Keine Dokumente ausgewählt"}, status_code=400)

    import pypdf

    conn = connection.get_connection()
    writer = pypdf.PdfWriter()
    skipped: list[str] = []
    merged = 0
    try:
        for doc_id in body.ids:
            path, err = _resolve_pdf_path(conn, doc_id)
            if err:
                row = conn.execute("SELECT filename FROM documents WHERE id = ?", (doc_id,)).fetchone()
                name = row["filename"] if row else str(doc_id)
                skipped.append(f"{name} ({err})")
                continue
            try:
                writer.append(str(path))
                merged += 1
            except Exception as e:
                skipped.append(f"{path.name} (nicht lesbar: {e})")
    finally:
        conn.close()

    if merged == 0:
        writer.close()
        return JSONResponse(
            {"ok": False, "error": "Keine gültigen PDFs in der Auswahl", "skipped": skipped},
            status_code=400,
        )

    buf = io.BytesIO()
    writer.write(buf)
    writer.close()

    return JSONResponse({
        "ok": True,
        "merged": merged,
        "skipped": skipped,
        "pdf_base64": base64.b64encode(buf.getvalue()).decode("ascii"),
    })
