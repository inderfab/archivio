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
        "request": request,
        "projects": projects,
    })


@app.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    q: str          = Query(default=""),
    project_id: str = Query(default=""),
    type: str       = Query(default=""),
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
        "query": q,
        "total": total,
        "error": error,
    })


@app.get("/projects", response_class=HTMLResponse)
async def projects_page(request: Request):
    conn = connection.get_connection()
    rows = conn.execute(
        "SELECT id, name, path, active FROM projects ORDER BY name"
    ).fetchall()
    conn.close()
    return templates.TemplateResponse("projects.html", {
        "request": request,
        "projects": rows,
    })


@app.get("/open")
async def open_file(path: str = Query(...)):
    """Öffnet eine lokale Datei mit dem Standard-Programm (macOS open)."""
    try:
        subprocess.run(["open", path], check=True, timeout=5)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── Such-Logik ────────────────────────────────────────────────────────────────

def _search(conn, q: str, project_id: str, ext: str):
    filters, filter_params = _build_filters(project_id, ext)

    # Schritt 1: FTS5 Prefix-Suche
    results, error = _search_fts(conn, q, filters, filter_params)
    if results or error:
        return results, error

    # Schritt 2: LIKE-Fallback wenn FTS nichts liefert
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
            d.extraction_status,
            p.name   AS project_name,
            dp.path  AS filepath,
            dc.content AS raw_content
        FROM documents_fts
        JOIN documents      d  ON documents_fts.rowid = d.id
        JOIN projects       p  ON p.id = d.project_id
        JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        LEFT JOIN document_content dc ON dc.document_id = d.id
        WHERE documents_fts MATCH ?
        {filters}
        ORDER BY rank
        LIMIT 50
    """
    try:
        rows = conn.execute(sql, [fts_q] + filter_params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["fallback"] = False
            d["excerpt"] = _excerpt(d.pop("raw_content") or "", q)
            results.append(d)
        return results, None
    except Exception as exc:
        return [], str(exc)


def _search_like(conn, q: str, filters: str, filter_params: list):
    """LIKE-Fallback: langsamer, findet aber Teilbegriffe mitten im Wort."""
    words = [re.sub(r'["\(\)\*\:\^]', "", w) for w in q.split() if w]
    if not words:
        return [], None

    # Jedes Wort muss in content ODER filename vorkommen
    like_clauses = " AND ".join(
        "(COALESCE(dc.content,'') LIKE ? OR d.filename LIKE ?)"
        for _ in words
    )
    like_params = [p for w in words for p in (f"%{w}%", f"%{w}%")]

    sql = f"""
        SELECT
            d.id, d.filename, d.extension, d.filesize, d.modified_at,
            d.extraction_status,
            p.name   AS project_name,
            dp.path  AS filepath,
            dc.content AS raw_content
        FROM documents d
        JOIN projects       p  ON p.id = d.project_id
        JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        LEFT JOIN document_content dc ON dc.document_id = d.id
        WHERE {like_clauses}
        {filters}
        LIMIT 50
    """
    try:
        rows = conn.execute(sql, like_params + filter_params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["fallback"] = True
            d["excerpt"] = _excerpt(d.pop("raw_content") or "", q)
            results.append(d)
        return results, None
    except Exception as exc:
        return [], str(exc)


def _excerpt(text: str, query: str, window: int = 220) -> str:
    """Kurzen Ausschnitt mit <mark>-Hervorhebungen erzeugen."""
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

    start = max(0, pos - 60)
    end   = min(len(text), start + window)
    snippet = text[start:end]

    for w in words:
        snippet = re.sub(
            f"({re.escape(w)})",
            r"<mark>\1</mark>",
            snippet,
            flags=re.IGNORECASE,
        )

    return ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")


def _make_fts_query(q: str) -> str:
    """Jedes Wort als Prefix-Query, mit AND verknüpft.

    'Flurhof Statik' → 'Flurhof* AND Statik*'
    Einzelwort ohne Quotes vermeidet Phrase-Query-Interpretation durch FTS5.
    """
    words = [re.sub(r'["\(\)\*\:\^]', "", w) for w in q.split()]
    words = [w for w in words if w]
    if not words:
        return '""'
    return " AND ".join(f"{w}*" for w in words)
