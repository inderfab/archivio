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
from fastapi.templating import Jinja2Templates

from db import connection

app = FastAPI(title="Archivio")
app.mount("/static", StaticFiles(directory="web/static"), name="static")
templates = Jinja2Templates(directory="web/templates")

# ── Jinja2 Filter ─────────────────────────────────────────────────────────────

def _fmt_date(iso: str | None) -> str:
    if not iso:
        return "—"
    return iso[:10]

def _fmt_size(n: int) -> str:
    n = n or 0
    if n < 1024:       return f"{n} B"
    if n < 1_048_576:  return f"{n / 1024:.0f} KB"
    return f"{n / 1_048_576:.1f} MB"

def _urlencode(v: str) -> str:
    return quote(str(v), safe="")

templates.env.filters["fmt_date"]   = _fmt_date
templates.env.filters["fmt_size"]   = _fmt_size
templates.env.filters["urlencode"]  = _urlencode

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
    fts_q  = _make_fts_query(q)
    params: list = [fts_q]
    filters = ""

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

    sql = f"""
        SELECT
            d.id,
            d.filename,
            d.extension,
            d.filesize,
            d.modified_at,
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
        rows = conn.execute(sql, params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
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
    words = [re.sub(r'["\(\)\*\:\^]', "", w) for w in q.split()]
    words = [w for w in words if w]
    if not words:
        return '""'
    return " ".join(f'"{w}"*' for w in words)
