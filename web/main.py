from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from db import connection
from web.shared import templates
from web.dashboard import router as dashboard_router
from web.api import router as api_router


# ── Scheduler ─────────────────────────────────────────────────────────────────

def _scheduler_loop():
    triggered_today: str | None = None
    while True:
        try:
            from config import settings
            scan_time = (settings.get("scheduler.scan_time") or "").strip()
            if scan_time:
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                if now.strftime("%H:%M") == scan_time and triggered_today != today:
                    triggered_today = today
                    try:
                        import requests as _req
                        _req.post("http://127.0.0.1:8000/api/scan/all", timeout=5)
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    yield


app = FastAPI(title="Archivio", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="web/static"), name="static")
app.include_router(dashboard_router)
app.include_router(api_router)

# ── KI-Suche ──────────────────────────────────────────────────────────────────

@app.get("/search/ai", response_class=HTMLResponse)
async def search_ai(
    request:    Request,
    q:          str = Query(default=""),
    project_id: str = Query(default=""),
):
    if not q.strip():
        return HTMLResponse("")

    from scanner.embedder import ai_status, embed_query, vector_search, llm_answer

    status = ai_status()
    if not status["ok"]:
        return templates.TemplateResponse("_ai_answer.html", {
            "request":  request,
            "question": q,
            "answer":   None,
            "sources":  [],
            "error":    status["reason"],
        })

    conn  = connection.get_connection()
    qvec  = embed_query(q)
    if qvec is None:
        conn.close()
        return templates.TemplateResponse("_ai_answer.html", {
            "request": request, "question": q, "answer": None, "sources": [],
            "error": "Fehler beim Einbetten der Frage. Ist Ollama erreichbar?",
        })

    sources = vector_search(conn, qvec, project_id=project_id)

    error = None
    if not sources:
        has_embeddings = conn.execute(
            "SELECT 1 FROM document_chunks WHERE embedding IS NOT NULL LIMIT 1"
        ).fetchone()
        error = (
            "Keine eingebetteten Dokumente gefunden. Bitte zuerst Embeddings generieren."
            if not has_embeddings else
            "Keine relevanten Dokumente für diese Frage gefunden."
        )

    conn.close()
    answer = llm_answer(q, sources) if sources else None

    return templates.TemplateResponse("_ai_answer.html", {
        "request":  request,
        "question": q,
        "answer":   answer,
        "sources":  sources,
        "error":    error,
    })

# ── Routen ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    conn = connection.get_connection()
    projects = conn.execute(
        "SELECT id, name FROM projects WHERE active=1 ORDER BY name"
    ).fetchall()
    conn.close()
    return templates.TemplateResponse("index.html", {
        "request":  request,
        "projects": projects,
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
):
    results, error, total = [], None, 0
    has_filters = any([from_addr, to_addr, subject_filter, date_from, date_to, filesize, duplicates_only])
    if q.strip() or has_filters:
        conn = connection.get_connection()
        filters_str, filter_params = _build_filters(
            project_id, type, from_addr, to_addr, subject_filter,
            date_from, date_to, filesize, duplicates_only,
        )
        results, error = _search(conn, q.strip(), filters_str, filter_params)
        total = len(results)
        conn.close()
    return templates.TemplateResponse("search_results.html", {
        "request": request,
        "results": results,
        "query":   q,
        "total":   total,
        "error":   error,
    })



@app.get("/open")
async def open_file(path: str = Query(...)):
    try:
        subprocess.run(["open", path], check=True, timeout=5)
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
    """Öffnet Mail in Apple Mail via message:// URL-Schema."""
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
    try:
        subprocess.run(["open", url], check=True, timeout=5)
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

def _search(conn, q: str, filters: str, filter_params: list):
    if not q:
        return _search_filtered(conn, filters, filter_params)
    results, error = _search_fts(conn, q, filters, filter_params)
    if results or error:
        return results, error
    results, error = _search_like(conn, q, filters, filter_params)
    for r in results:
        r["fallback"] = True
    return results, error


def _build_filters(
    project_id: str, ext: str,
    from_addr: str = "", to_addr: str = "", subject_filter: str = "",
    date_from: str = "", date_to: str = "",
    filesize: str = "", duplicates_only: str = "",
) -> tuple[str, list]:
    filters, params = "", []
    if project_id:
        try:
            filters += " AND d.project_id = ?"
            params.append(int(project_id))
        except ValueError:
            pass
    if ext == "mail":
        filters += " AND d.source_type = 'email'"
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
               p.name      AS project_name,
               dp.path     AS filepath,
               dc.content  AS raw_content,
               m.sender    AS mail_sender,
               m.date      AS mail_date
        FROM documents d
        JOIN  projects        p  ON p.id  = d.project_id
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
            p.name      AS project_name,
            dp.path     AS filepath,
            dc_chunk.content AS raw_content,
            dc_chunk.page_number,
            chunks_fts.rank,
            m.sender    AS mail_sender,
            m.date      AS mail_date
        FROM chunks_fts
        JOIN  document_chunks dc_chunk ON chunks_fts.rowid = dc_chunk.id
        JOIN  documents       d        ON d.id = dc_chunk.document_id
        JOIN  projects        p        ON p.id = d.project_id
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
            p.name      AS project_name,
            dp.path     AS filepath,
            dc.content  AS raw_content,
            m.sender    AS mail_sender,
            m.date      AS mail_date
        FROM documents d
        JOIN  projects        p  ON p.id  = d.project_id
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


def _make_fts_query(q: str) -> str:
    words = [re.sub(r'["\(\)\*\:\^]', "", w) for w in q.split()]
    words = [w for w in words if w]
    if not words:
        return '""'
    return " AND ".join(f"{w}*" for w in words)
