from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from db import connection
from web.shared import templates
from web.dashboard import router as dashboard_router

app = FastAPI(title="Archivio")
app.mount("/static", StaticFiles(directory="web/static"), name="static")
app.include_router(dashboard_router)

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
    request:    Request,
    q:          str = Query(default=""),
    project_id: str = Query(default=""),
    type:       str = Query(default=""),
):
    results, error, total = [], None, 0
    if q.strip():
        conn = connection.get_connection()
        results, error = _search(conn, q.strip(), project_id, type)
        total = len(results)
        conn.close()
    return templates.TemplateResponse("search_results.html", {
        "request": request,
        "results": results,
        "query":   q,
        "total":   total,
        "error":   error,
    })


@app.get("/projects", response_class=HTMLResponse)
async def projects_page(request: Request):
    conn = connection.get_connection()
    rows = conn.execute(
        "SELECT id, name, path, active FROM projects ORDER BY name"
    ).fetchall()
    conn.close()
    return templates.TemplateResponse("projects.html", {
        "request":  request,
        "projects": rows,
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

def _search(conn, q: str, project_id: str, ext: str):
    filters, filter_params = _build_filters(project_id, ext)
    results, error = _search_fts(conn, q, filters, filter_params)
    if results or error:
        return results, error
    results, error = _search_like(conn, q, filters, filter_params)
    for r in results:
        r["fallback"] = True
    return results, error


def _build_filters(project_id: str, ext: str) -> tuple[str, list]:
    filters, params = "", []
    if project_id:
        try:
            filters += " AND d.project_id = ?"
            params.append(int(project_id))
        except ValueError:
            pass
    if ext:
        e = ext if ext.startswith(".") else f".{ext}"
        filters += " AND d.extension = ?"
        params.append(e)
    return filters, params


def _search_fts(conn, q: str, filters: str, filter_params: list):
    fts_q = _make_fts_query(q)
    sql = f"""
        SELECT
            d.id, d.filename, d.extension, d.filesize, d.modified_at,
            d.extraction_status, d.source_type,
            p.name      AS project_name,
            dp.path     AS filepath,
            dc.content  AS raw_content,
            m.sender    AS mail_sender,
            m.date      AS mail_date
        FROM documents_fts
        JOIN  documents       d  ON documents_fts.rowid = d.id
        JOIN  projects        p  ON p.id  = d.project_id
        LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        LEFT JOIN document_content dc ON dc.document_id = d.id
        LEFT JOIN mails       m  ON m.document_id = d.id
        WHERE documents_fts MATCH ?
        {filters}
        ORDER BY rank
        LIMIT 50
    """
    try:
        rows    = conn.execute(sql, [fts_q] + filter_params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["fallback"] = False
            d["excerpt"]  = _excerpt(d.pop("raw_content") or "", q)
            results.append(d)
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
            d["fallback"] = True
            d["excerpt"]  = _excerpt(d.pop("raw_content") or "", q)
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
