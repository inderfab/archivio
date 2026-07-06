from __future__ import annotations

import logging
import logging.handlers
import os
import re
import signal
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Logging ───────────────────────────────────────────────────────────────────
# Konfiguriert den Root-Logger so dass Scanner/Extractor-Logs in server.log
# erscheinen (dasselbe File das uvicorn via stdout/stderr beschreibt).

def _setup_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return  # bereits konfiguriert (z.B. im Dev-Modus via --reload)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # uvicorn-Logger nicht doppelt ausgeben
    logging.getLogger("uvicorn.access").propagate = False

_setup_logging()

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from db import connection
from web.shared import templates
from web.dashboard import router as dashboard_router
from web.api import router as api_router


# ── Scheduler ─────────────────────────────────────────────────────────────────

def _scheduler_loop():
    log = logging.getLogger("scheduler")
    triggered_today: str | None = None
    log.info("Scheduler-Loop gestartet")
    while True:
        try:
            from config import settings
            scan_time = (settings.get("scheduler.scan_time") or "").strip()
            if scan_time:
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                # 2-Minuten-Fenster: verhindert dass sleep(60)-Drift die Minute verpasst
                h, m = map(int, scan_time.split(":"))
                target = now.replace(hour=h, minute=m, second=0, microsecond=0)
                diff = abs((now - target).total_seconds())
                if diff < 120 and triggered_today != today:
                    triggered_today = today
                    port = settings.get("server.port", 8000)
                    log.info("Geplanter Scan um %s wird ausgelöst (Port %s)", scan_time, port)
                    try:
                        import requests as _req
                        r = _req.post(f"http://127.0.0.1:{port}/api/scan/all", timeout=10)
                        log.info("Geplanter Scan gestartet: HTTP %s %s", r.status_code, r.text[:200])
                    except Exception as exc:
                        log.error("Geplanter Scan fehlgeschlagen: %s", exc)
        except Exception as exc:
            log.error("Scheduler-Fehler: %s", exc)
        time.sleep(60)


def _resume_embeddings_on_startup():
    """Beim Start: fehlende Embeddings nachholen falls Ollama läuft.
    Sicherheitsnetz falls der Server während eines Embedding-Laufs abgestürzt ist."""
    time.sleep(8)  # Server erst vollständig hochfahren lassen
    try:
        from scanner.embedder import is_ollama_running
        if not is_ollama_running():
            return
        conn = connection.get_connection()
        try:
            pending = conn.execute(
                "SELECT COUNT(*) FROM document_chunks WHERE embedding IS NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        if pending > 0:
            logging.getLogger(__name__).info(
                "Startup: %d Chunks ohne Embedding — Backfill startet", pending
            )
            from web.dashboard import _run_post_scan_embedding
            _run_post_scan_embedding()
    except Exception as exc:
        logging.getLogger(__name__).debug("Startup-Embedding-Check: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Migrations beim Start ausführen — auch bei bestehenden DBs
    try:
        from db import migrations as _mig
        _conn = connection.get_connection()
        _mig.run(_conn)
        _conn.close()
    except Exception as _e:
        import logging
        logging.getLogger(__name__).warning("Migration fehlgeschlagen: %s", _e)
    # SIGTERM → alle Scanner-Worker sofort killen bevor Prozess endet
    def _sigterm(sig, frame):
        from scanner.walker import kill_all_workers
        kill_all_workers()
        raise SystemExit(0)
    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except Exception:
        pass

    threading.Thread(target=_scheduler_loop, daemon=True).start()
    threading.Thread(target=_resume_embeddings_on_startup, daemon=True).start()
    yield
    # Shutdown: alle Worker killen
    from scanner.walker import kill_all_workers
    kill_all_workers()


app = FastAPI(title="Archivio", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="web/static"), name="static")
app.include_router(dashboard_router)
app.include_router(api_router)

# ── KI-Suche ──────────────────────────────────────────────────────────────────

def _ai_vector_search(q: str, project_id: str):
    """Gemeinsame Logik: Embeddings + Vektorsuche. Gibt (sources, error, ollama_missing) zurück.
    Läuft synchron — immer via run_in_executor aufrufen, nicht direkt aus async-Handler!
    """
    from scanner.embedder import ai_status, embed_query, vector_search, keyword_search_chunks

    status = ai_status()
    if not status["ok"]:
        return None, status["reason"], status.get("ollama_missing", False)

    conn = connection.get_connection()
    try:
        qvec = embed_query(q)
        if qvec is None:
            return None, "Fehler beim Einbetten der Frage. Ist Ollama erreichbar?", False

        kw_sources  = keyword_search_chunks(conn, q, project_id=project_id, limit=10)
        vec_sources = vector_search(conn, qvec, project_id=project_id, limit=20)
        # Hybrid-Merge: Keyword zuerst (präziser), Vektor ergänzt
        seen_ids = {s["id"] for s in kw_sources}
        sources  = kw_sources + [s for s in vec_sources if s["id"] not in seen_ids]
        # De-duplizieren auf Dokument-Ebene: pro Dokument nur besten Chunk
        seen_docs: dict = {}
        deduped = []
        for s in sources:
            doc_id = s["document_id"]
            if doc_id not in seen_docs:
                seen_docs[doc_id] = s
                deduped.append(s)
        sources = deduped[:12]

        error = None
        if not sources:
            if project_id:
                try:
                    has_project_emb = conn.execute(
                        """SELECT 1 FROM document_chunks dc
                           JOIN documents d ON d.id = dc.document_id
                           WHERE dc.embedding IS NOT NULL AND d.project_id = ? LIMIT 1""",
                        (int(project_id),)
                    ).fetchone()
                except (ValueError, TypeError):
                    has_project_emb = None
                if not has_project_emb:
                    proj_name = conn.execute(
                        "SELECT name FROM projects WHERE id = ?", (project_id,)
                    ).fetchone()
                    name = proj_name["name"] if proj_name else f"Projekt {project_id}"
                    error = f"Keine Embeddings für «{name}». Embeddings generieren und nochmals versuchen."
                else:
                    error = "Keine relevanten Dokumente für diese Frage gefunden."
            else:
                has_embeddings = conn.execute(
                    "SELECT 1 FROM document_chunks WHERE embedding IS NOT NULL LIMIT 1"
                ).fetchone()
                error = (
                    "Keine eingebetteten Dokumente gefunden. Bitte zuerst Embeddings generieren."
                    if not has_embeddings else
                    "Keine relevanten Dokumente für diese Frage gefunden."
                )

        return sources, error, False
    finally:
        conn.close()


@app.get("/search/ai", response_class=HTMLResponse)
async def search_ai(
    request:    Request,
    q:          str = Query(default=""),
    project_id: str = Query(default=""),
):
    """Schritt 1 (schnell): Vektorsuche → Quellen sofort anzeigen. Antwort lädt separat nach."""
    if not q.strip():
        return HTMLResponse("")

    import asyncio
    loop = asyncio.get_event_loop()
    sources, error, ollama_missing = await loop.run_in_executor(
        None, _ai_vector_search, q, project_id
    )

    if error and sources is None:
        return templates.TemplateResponse("_ai_answer.html", {
            "request": request, "question": q, "answer": None, "sources": [],
            "error": error, "ollama_missing": ollama_missing,
        })

    return templates.TemplateResponse("_ai_sources.html", {
        "request":    request,
        "question":   q,
        "project_id": project_id,
        "sources":    sources or [],
        "error":      error,
    })


@app.get("/search/ai/answer", response_class=HTMLResponse)
async def search_ai_answer(
    request:    Request,
    q:          str = Query(default=""),
    project_id: str = Query(default=""),
):
    """Schritt 2 (langsam): LLM-Antwort generieren."""
    if not q.strip():
        return HTMLResponse("")

    import asyncio
    from scanner.embedder import llm_answer

    loop = asyncio.get_event_loop()
    sources, error, _ = await loop.run_in_executor(
        None, _ai_vector_search, q, project_id
    )
    if not sources:
        return HTMLResponse('<div class="ai-no-answer">Keine Antwort generiert — keine relevanten Quellen gefunden.</div>')

    answer = await loop.run_in_executor(None, llm_answer, q, sources)
    if answer and sources:
        sources = _rerank_by_answer(sources, answer)

    return templates.TemplateResponse("_ai_answer_only.html", {
        "request":  request,
        "question": q,
        "answer":   answer,
        "sources":  sources[:3],   # Top-3 nach Reranking — als Quellenangabe
    })

# ── Routen ────────────────────────────────────────────────────────────────────

def _build_project_groups(conn) -> list[dict]:
    """Gibt Projekte zurück, annotiert mit Eltern-Info für den Suche-Dropdown."""
    rows = conn.execute(
        "SELECT id, name, path FROM projects WHERE active=1 ORDER BY path"
    ).fetchall()
    projects = [dict(r) for r in rows]
    # Eltern-Projekt bestimmen: längstes path-Prefix das selbst ein Projekt ist
    for p in projects:
        parent = None
        best = 0
        for other in projects:
            if other["id"] == p["id"]:
                continue
            if p["path"].startswith(other["path"] + "/") and len(other["path"]) > best:
                parent = other
                best = len(other["path"])
        p["parent_id"]   = parent["id"]   if parent else None
        p["parent_name"] = parent["name"] if parent else None
    # Kinder pro Eltern sammeln
    children: dict[int, list] = {}
    top: list[dict] = []
    for p in projects:
        if p["parent_id"] is not None:
            children.setdefault(p["parent_id"], []).append(p)
        else:
            top.append(p)
    # Resultat: Top-Level-Projekte mit ihren Kindern
    result = []
    for p in top:
        p["children"] = children.get(p["id"], [])
        result.append(p)
    # Verwaiste Sub-Projekte (Eltern nicht aktiv) ans Ende
    all_top_ids = {p["id"] for p in top}
    for p in projects:
        if p["parent_id"] is not None and p["parent_id"] not in all_top_ids:
            p["children"] = []
            result.append(p)
    return result


def _mailbox_display_name(mailbox_name: str) -> str:
    last = mailbox_name.split("/")[-1].strip()
    return "Inbox" if last.upper() == "INBOX" else last


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    conn = connection.get_connection()
    projects = _build_project_groups(conn)
    raw_mailboxes = conn.execute(
        "SELECT mailbox_name FROM mail_scan_config WHERE active=1 AND project_id IS NULL ORDER BY mailbox_name"
    ).fetchall()
    total_docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    conn.close()
    mail_mailboxes = [
        {"mailbox_name": r["mailbox_name"], "display_name": _mailbox_display_name(r["mailbox_name"])}
        for r in raw_mailboxes
    ]
    return templates.TemplateResponse("index.html", {
        "request":        request,
        "projects":       projects,
        "mail_mailboxes": mail_mailboxes,
        "total_docs":     total_docs,
    })


@app.get("/search", response_class=HTMLResponse)
async def search(
    request:         Request,
    q:               str = Query(default=""),
    project_id:      str = Query(default=""),
    type:            str = Query(default=""),
    from_addr:       str = Query(default=""),
    to_addr:         str = Query(default=""),
    subject_filter:  str = Query(default=""),
    date_from:       str = Query(default=""),
    date_to:         str = Query(default=""),
    filesize:        str = Query(default=""),
    duplicates_only: str = Query(default=""),
    search_in:       str = Query(default="docs"),
):
    scope = set(search_in.split(",")) if search_in else {"docs"}
    if not scope:
        scope = {"docs"}
    filter_plans     = "plans" in scope
    content_scope    = scope - {"plans"} or {"docs"}  # mindestens docs wenn nur plans aktiv
    search_docs      = "docs" in content_scope
    search_filenames = "filenames" in content_scope
    search_folders   = "folders" in content_scope

    results, error, total = [], None, 0
    folder_results = []
    has_filters = any([from_addr, to_addr, subject_filter, date_from, date_to, filesize, duplicates_only])
    if q.strip() or has_filters:
        conn = connection.get_connection()
        filters_str, filter_params = _build_filters(
            project_id, type, from_addr, to_addr, subject_filter,
            date_from, date_to, filesize, duplicates_only,
            filter_plans=filter_plans,
        )
        if search_docs and search_filenames:
            results, error = _search(conn, q.strip(), filters_str, filter_params)
        elif search_docs:
            if q.strip():
                results, error = _search_fts(conn, q.strip(), filters_str, filter_params)
                if not results and not error:
                    results, error = _search_like(conn, q.strip(), filters_str, filter_params)
                    for r in results:
                        r["fallback"] = True
            else:
                results, error = _search_filtered(conn, filters_str, filter_params)
        elif search_filenames:
            if q.strip():
                results = _search_filename(conn, q.strip(), filters_str, filter_params)
                results.sort(key=lambda r: _filename_score(r.get("filename", ""), q.strip()), reverse=True)
                error = None
            else:
                results, error = _search_filtered(conn, filters_str, filter_params)
        if search_folders and q.strip():
            folder_results = _search_folders(conn, q.strip(), project_id)
        total = len(results)
        conn.close()
    return templates.TemplateResponse("search_results.html", {
        "request":        request,
        "results":        results,
        "query":          q,
        "total":          total,
        "error":          error,
        "folder_results": folder_results,
    })



@app.get("/open")
async def open_file(path: str = Query(...)):
    try:
        result = subprocess.run(["open", path], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return JSONResponse({"ok": False, "error": result.stderr or result.stdout}, status_code=500)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)



@app.get("/reveal")
async def reveal_file(path: str = Query(...)):
    try:
        subprocess.run(["open", "-R", path], check=True, timeout=5)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/open/mail/{document_id}")
async def open_mail(document_id: int):
    """Gibt message://-URL zurück — der Helper öffnet sie lokal auf dem Client-Mac."""
    conn = connection.get_connection()
    row  = conn.execute(
        "SELECT hash FROM documents WHERE id=? AND source_type='email'",
        (document_id,),
    ).fetchone()
    conn.close()
    if not row:
        return JSONResponse({"ok": False, "error": "Mail nicht gefunden"}, status_code=404)
    mid = row["hash"].strip("<>").strip()
    url = f"message://%3C{quote(mid, safe='')}%3E"
    return JSONResponse({"ok": True, "url": url})


@app.post("/api/open/mail/{document_id}")
async def open_mail_server(document_id: int):
    """Öffnet Mail direkt auf dem Server-Mac (Fallback wenn kein Helper läuft)."""
    conn = connection.get_connection()
    row  = conn.execute(
        "SELECT hash FROM documents WHERE id=? AND source_type='email'",
        (document_id,),
    ).fetchone()
    conn.close()
    if not row:
        return JSONResponse({"ok": False, "error": "Mail nicht gefunden"}, status_code=404)
    mid = row["hash"].strip("<>").strip()
    url = f"message://<{mid}>"
    try:
        subprocess.run(["open", url], timeout=5, check=False)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/preview/{document_id}", response_class=HTMLResponse)
async def preview(request: Request, document_id: int, q: str = Query(default="")):
    """Lazy-Vorschau für ein Suchergebnis."""
    conn = connection.get_connection()
    doc  = conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
    if not doc:
        conn.close()
        return HTMLResponse("")
    doc = dict(doc)
    content_row = conn.execute(
        "SELECT content FROM document_content WHERE document_id=?", (document_id,)
    ).fetchone()
    raw     = (content_row["content"] if content_row else "") or ""
    excerpt = raw[:600].rstrip() + ("…" if len(raw) > 600 else "")
    if q:
        for w in [re.sub(r'["\(\)\*\:\^]', "", x) for x in q.split() if x]:
            excerpt = re.sub(
                f"({re.escape(w)})", r"<mark>\1</mark>",
                excerpt, flags=re.IGNORECASE,
            )

    ctx: dict = {"request": request, "doc": doc, "excerpt": excerpt, "q": q}

    if doc["source_type"] == "email":
        mail = conn.execute(
            "SELECT * FROM mails WHERE document_id=?", (document_id,)
        ).fetchone()
        ctx["mail"] = dict(mail) if mail else {}

    conn.close()
    return templates.TemplateResponse("_preview.html", ctx)


# ── Such-Logik ────────────────────────────────────────────────────────────────

def _rerank_by_answer(sources: list, answer: str) -> list:
    """Sortiert Quellen: der Chunk mit den meisten Antwort-Wörtern kommt zuerst."""
    answer_words = {w for w in re.findall(r'\b\w{4,}\b', answer.lower()) if w}
    if not answer_words:
        return sources

    def score(s):
        content = (s.get("content") or "").lower()
        return sum(1 for w in answer_words if w in content)

    return sorted(sources, key=score, reverse=True)


def _filename_score(filename: str, q: str) -> int:
    """Höherer Score = bessere Übereinstimmung mit Dateiname.
    Stufen pro Suchwort: exakter Stem-Treffer (100) > Stem beginnt mit Wort (40)
    > ganzes Token im Stem (20) > Substring irgendwo (5).
    """
    words = [re.sub(r'["\(\)\*\:\^]', "", w) for w in q.lower().split()]
    words = [w for w in words if w and w not in _STOPWORDS]
    if not words:
        return 0
    stem = re.sub(r'\.[^.]+$', '', filename.lower())  # extension entfernen
    fn   = filename.lower()
    score = 0
    for w in words:
        if stem == w:
            score += 100
        elif stem.startswith(w):
            score += 40
        elif re.search(r'(?<![a-z0-9À-ÿ])' + re.escape(w) + r'(?![a-z0-9À-ÿ])', stem):
            score += 20
        elif w in fn:
            score += 5
    return score

def _search(conn, q: str, filters: str, filter_params: list):
    if not q:
        return _search_filtered(conn, filters, filter_params)
    results, error = _search_fts(conn, q, filters, filter_params)
    if error:
        return results, error
    # Dateinamen-Treffer immer zusätzlich prüfen — chunks_fts enthält keine Dateinamen
    fname_results = _search_filename(conn, q, filters, filter_params)
    seen = {r["id"] for r in results}
    for r in fname_results:
        if r["id"] not in seen:
            results.append(r)
    # Filename-Treffer nach oben schieben
    results.sort(key=lambda r: _filename_score(r.get("filename", ""), q), reverse=True)
    if results:
        return results, None
    results, error = _search_like(conn, q, filters, filter_params)
    for r in results:
        r["fallback"] = True
    return results, error


_TYPE_CATEGORIES: dict[str, list[str]] = {
    "cat:bilder":    [".png", ".jpg", ".jpeg", ".tiff", ".tif", ".heic", ".heif",
                      ".webp", ".gif", ".bmp", ".svg", ".raw", ".cr2", ".nef", ".arw", ".dng"],
    "cat:cad":       [".dwg", ".dxf", ".pln", ".vwx", ".ifc", ".rvt", ".skp", ".3dm", ".nwd"],
    "cat:3d":        [".step", ".stp", ".stl", ".3ds", ".obj", ".fbx", ".c4d"],
    "cat:adobe":     [".psd", ".ai", ".indd"],
    "cat:video":     [".mp4", ".mov", ".avi", ".mkv", ".m4v", ".wmv",
                      ".mp3", ".wav", ".aac", ".m4a", ".flac"],
    "cat:dokumente": [".pdf", ".docx", ".doc", ".xlsx", ".xls", ".xlsm",
                      ".pptx", ".ppt", ".txt", ".rtf", ".csv", ".eml", ".msg"],
}


def _build_filters(
    project_id: str, ext: str,
    from_addr: str = "", to_addr: str = "", subject_filter: str = "",
    date_from: str = "", date_to: str = "",
    filesize: str = "", duplicates_only: str = "",
    filter_plans: bool = False,
) -> tuple[str, list]:
    filters, params = "", []
    if filter_plans:
        filters += " AND json_extract(d.metadata, '$.is_plan') = 1"
    if project_id:
        if project_id.startswith("mailbox:"):
            filters += " AND m.mailbox_name = ? AND d.project_id IS NULL"
            params.append(project_id[8:])
        else:
            try:
                # Eltern-Projekt und alle Sub-Projekte (Pfad-Prefix) einschliessen.
                # Zweite Bedingung: Dokumente die physisch im Sub-Projekt-Ordner liegen,
                # aber noch unter dem Eltern-Projekt indexiert sind (vor Sub-Projekt-Aktivierung).
                filters += (
                    " AND (d.project_id IN ("
                    "  SELECT p2.id FROM projects p2"
                    "  JOIN projects p1 ON (p2.path = p1.path OR p2.path LIKE (p1.path || '/%'))"
                    "  WHERE p1.id = ?"
                    ") OR d.id IN ("
                    "  SELECT dp2.document_id FROM document_paths dp2"
                    "  JOIN projects p3 ON dp2.path LIKE (p3.path || '/%')"
                    "  WHERE p3.id = ?"
                    "))"
                )
                params.extend([int(project_id), int(project_id)])
            except ValueError:
                pass
    if ext == "mail":
        filters += " AND d.source_type = 'email'"
    elif ext in _TYPE_CATEGORIES:
        exts = _TYPE_CATEGORIES[ext]
        placeholders = ",".join("?" * len(exts))
        filters += f" AND d.extension IN ({placeholders})"
        params.extend(exts)
    elif ext:
        e = ext if ext.startswith(".") else f".{ext}"
        filters += " AND d.extension = ?"
        params.append(e)
    if from_addr:
        filters += " AND m.sender LIKE ?"
        params.append(f"%{from_addr}%")
    if to_addr:
        filters += " AND (m.recipients LIKE ? OR m.cc LIKE ?)"
        params.extend([f"%{to_addr}%", f"%{to_addr}%"])
    if subject_filter:
        filters += " AND m.subject LIKE ?"
        params.append(f"%{subject_filter}%")
    if date_from:
        filters += " AND d.modified_at >= ?"
        params.append(date_from)
    if date_to:
        filters += " AND d.modified_at <= ?"
        params.append(date_to + "T23:59:59")
    if filesize:
        try:
            filters += " AND d.filesize > ?"
            params.append(int(filesize) * 1024 * 1024)
        except ValueError:
            pass
    if duplicates_only:
        filters += (" AND d.id IN ("
                    "SELECT document_id FROM document_paths "
                    "GROUP BY document_id HAVING COUNT(*) > 1)")
    return filters, params


def _search_filtered(conn, filters: str, filter_params: list):
    sql = f"""
        SELECT d.id, d.filename, d.extension, d.filesize, d.modified_at,
               d.extraction_status, d.source_type,
               COALESCE(p.name, m.mailbox_name) AS project_name,
               dp.path     AS filepath,
               dc.content  AS raw_content,
               m.sender    AS mail_sender,
               m.date      AS mail_date
        FROM documents d
        LEFT JOIN projects        p  ON p.id  = d.project_id
        LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        LEFT JOIN document_content dc ON dc.document_id = d.id
        LEFT JOIN mails       m  ON m.document_id = d.id
        WHERE 1=1 {filters}
        ORDER BY d.modified_at DESC
        LIMIT 50
    """
    try:
        rows = conn.execute(sql, filter_params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["fallback"]    = False
            d["page_number"] = None
            d["excerpt"]     = d.pop("raw_content") or ""
            results.append(d)
        return results, None
    except Exception as exc:
        return [], str(exc)


def _search_fts(conn, q: str, filters: str, filter_params: list):
    fts_q = _make_fts_query(q)
    sql = f"""
        SELECT
            d.id, d.filename, d.extension, d.filesize, d.modified_at,
            d.extraction_status, d.source_type,
            COALESCE(p.name, m.mailbox_name) AS project_name,
            dp.path     AS filepath,
            dc_chunk.content AS raw_content,
            dc_chunk.page_number,
            chunks_fts.rank,
            m.sender    AS mail_sender,
            m.date      AS mail_date
        FROM chunks_fts
        JOIN  document_chunks dc_chunk ON chunks_fts.rowid = dc_chunk.id
        JOIN  documents       d        ON d.id = dc_chunk.document_id
        LEFT JOIN projects        p        ON p.id = d.project_id
        LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        LEFT JOIN mails       m  ON m.document_id = d.id
        WHERE chunks_fts MATCH ?
        {filters}
        ORDER BY rank
        LIMIT 200
    """
    try:
        rows    = conn.execute(sql, [fts_q] + filter_params).fetchall()
        seen    = set()
        results = []
        for r in rows:
            d = dict(r)
            if d["id"] in seen:
                continue
            seen.add(d["id"])
            d["fallback"] = False
            d["excerpt"]  = _excerpt(d.pop("raw_content") or "", q)
            d.pop("rank", None)
            results.append(d)
            if len(results) >= 50:
                break
        return results, None
    except Exception as exc:
        return [], str(exc)


def _search_like(conn, q: str, filters: str, filter_params: list):
    words = [re.sub(r'["\(\)\*\:\^]', "", w) for w in q.split() if w]
    if not words:
        return [], None

    like_clauses = " AND ".join(
        "(COALESCE(dc.content,'') LIKE ? OR d.filename LIKE ?)"
        for _ in words
    )
    like_params = [p for w in words for p in (f"%{w}%", f"%{w}%")]

    sql = f"""
        SELECT
            d.id, d.filename, d.extension, d.filesize, d.modified_at,
            d.extraction_status, d.source_type,
            COALESCE(p.name, m.mailbox_name) AS project_name,
            dp.path     AS filepath,
            dc.content  AS raw_content,
            m.sender    AS mail_sender,
            m.date      AS mail_date
        FROM documents d
        LEFT JOIN projects        p  ON p.id  = d.project_id
        LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        LEFT JOIN document_content dc ON dc.document_id = d.id
        LEFT JOIN mails       m  ON m.document_id = d.id
        WHERE {like_clauses}
        {filters}
        LIMIT 50
    """
    try:
        rows    = conn.execute(sql, like_params + filter_params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["fallback"]    = True
            d["page_number"] = None
            d["excerpt"]     = _excerpt(d.pop("raw_content") or "", q)
            results.append(d)
        return results, None
    except Exception as exc:
        return [], str(exc)


def _excerpt(text: str, query: str, window: int = 220) -> str:
    if not text:
        return ""
    words = [re.sub(r'["\(\)\*\:\^]', "", w) for w in query.split()]
    words = [w for w in words if w]
    if not words:
        return text[:window] + ("…" if len(text) > window else "")

    text_lower = text.lower()
    pos = len(text)
    for w in words:
        idx = text_lower.find(w.lower())
        if idx != -1:
            pos = min(pos, idx)

    start   = max(0, pos - 60)
    end     = min(len(text), start + window)
    snippet = text[start:end]

    for w in words:
        snippet = re.sub(
            f"({re.escape(w)})", r"<mark>\1</mark>",
            snippet, flags=re.IGNORECASE,
        )

    return ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")


def _search_filename(conn, q: str, filters: str, filter_params: list) -> list[dict]:
    """Sucht per documents_fts nach Dateinamen-Treffern — nur filename-Spalte, nicht content."""
    # Column-Filter: nur filename-Spalte durchsuchen (documents_fts hat auch content)
    words = []
    for w in q.split():
        w = re.sub(r'["\(\)\*\:\^]', "", w)
        for part in w.split('.'):
            part = part.strip()
            if part and part.lower() not in _STOPWORDS:
                words.append(part)
    if not words:
        return []
    fts_q = " AND ".join(f"filename:{w}*" for w in words)
    sql = f"""
        SELECT
            d.id, d.filename, d.extension, d.filesize, d.modified_at,
            d.extraction_status, d.source_type,
            COALESCE(p.name, m.mailbox_name) AS project_name,
            dp.path     AS filepath,
            NULL        AS raw_content,
            NULL        AS page_number,
            m.sender    AS mail_sender,
            m.date      AS mail_date
        FROM documents_fts
        JOIN  documents       d  ON documents_fts.rowid = d.id
        LEFT JOIN projects        p  ON p.id = d.project_id
        LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        LEFT JOIN mails       m  ON m.document_id = d.id
        WHERE documents_fts MATCH ?
        {filters}
        LIMIT 50
    """
    try:
        rows = conn.execute(sql, [fts_q] + filter_params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["fallback"] = False
            d["excerpt"]  = ""
            d.pop("raw_content", None)
            results.append(d)
        return results
    except Exception:
        return []


def _search_folders(conn, q: str, project_id: str = "") -> list[dict]:
    """Findet Ordner deren Name die Suchbegriffe enthält (aus document_paths).

    Optimiert: Vorfilterung in SQL (Pfad muss alle Suchwörter enthalten) statt
    alle document_paths in den Speicher zu laden. folder.exists() (NAS-Stat) wird
    nur noch für die wenigen namentlich passenden Treffer aufgerufen, nicht für
    jeden Elternordner jedes Pfades.
    """
    words = [w.lower() for w in q.split() if len(w) > 1]
    if not words:
        return []
    try:
        # Grobfilter in SQL: nur Pfade die ALLE Suchwörter irgendwo enthalten —
        # notwendige Bedingung dafür, dass ein Ordnername alle Wörter enthält.
        sql = """
            SELECT DISTINCT dp.path, p.name AS project_name
            FROM document_paths dp
            JOIN documents d ON d.id = dp.document_id
            JOIN projects p ON p.id = d.project_id
            WHERE 1=1
        """
        params: list = []
        for w in words:
            sql += " AND lower(dp.path) LIKE ?"
            params.append(f"%{w}%")
        if project_id:
            try:
                pid  = int(project_id)
                prow = conn.execute("SELECT path FROM projects WHERE id=?", (pid,)).fetchone()
                if prow and prow["path"] and not str(prow["path"]).startswith("mailbox:"):
                    # Nur Ordner INNERHALB des Projektpfads. Wichtig wegen
                    # Hash-Deduplizierung: ein Dokument kann mehrere Pfade in
                    # verschiedenen Projekten haben (gleiche Datei) — d.project_id
                    # allein würde fremde Projektordner (Duplikate) durchlassen.
                    sql += " AND dp.path LIKE ?"
                    params.append(str(prow["path"]).rstrip("/") + "/%")
                else:
                    sql += " AND d.project_id = ?"
                    params.append(pid)
            except ValueError:
                pass
        sql += " LIMIT 2000"
        rows = conn.execute(sql, params).fetchall()

        seen_dirs: set[str] = set()
        results = []
        for row in rows:
            for folder in Path(row["path"]).parents:
                folder_str = str(folder)
                if folder_str in seen_dirs:
                    continue
                seen_dirs.add(folder_str)
                if all(w in folder.name.lower() for w in words) and folder.exists():
                    results.append({
                        "path":         folder_str,
                        "name":         folder.name,
                        "project_name": row["project_name"],
                    })
                    if len(results) >= 10:
                        return results
        return results
    except Exception:
        return []


_STOPWORDS = {
    "und", "oder", "der", "die", "das", "dem", "den", "des",
    "ein", "eine", "einen", "einem", "einer", "eines",
    "ist", "sind", "hat", "haben", "wird", "werden",
    "von", "bei", "mit", "zu", "in", "an", "auf", "für",
    "als", "auch", "aber", "noch", "nur", "schon",
    "wenn", "dann", "so", "wie", "was", "wer", "wo",
    "nicht", "kein", "keine", "keinen", "keinem",
    "sich", "es", "er", "sie", "wir", "ihr", "du", "ich",
}

def _make_fts_query(q: str) -> str:
    words = []
    for w in q.split():
        w = re.sub(r'["\(\)\*\:\^]', "", w)
        for part in w.split('.'):          # Punkt ist kein gültiges FTS5-Query-Zeichen
            part = part.strip()
            if part and part.lower() not in _STOPWORDS:
                words.append(part)
    if not words:
        return '""'
    if len(words) == 1:
        return f"{words[0]}*"

    and_query = " AND ".join(f"{w}*" for w in words)

    # Deutsche Komposita: "fenster liste" und "liste fenster" finden beide "Fensterliste".
    # Benachbarte Wortpaare in beiden Reihenfolgen zusammenkleben + vollständige Konkatenation.
    extras: list[str] = []
    for i in range(len(words) - 1):
        extras.append(f"{words[i]}{words[i + 1]}*")        # vorwärts
        extras.append(f"{words[i + 1]}{words[i]}*")        # rückwärts
    if len(words) > 2:
        extras.append("".join(words) + "*")

    return "(" + and_query + ") OR " + " OR ".join(extras)
