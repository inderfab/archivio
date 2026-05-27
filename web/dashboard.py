"""Dashboard: Projektverwaltung und Ordner-Browser."""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse

from config import settings
from db import connection
from scanner.walker import scan_project
from web.shared import templates

router = APIRouter(prefix="/dashboard")

# In-Memory Scan-Status  {project_id: {status, count, started_at, ...}}
_scans: dict[int, dict] = {}


# ── Dashboard-Hauptseite ──────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request):
    conn = connection.get_connection()
    connection.init_schema()          # stellt sicher dass Migration 002 läuft
    projects = _discovered_projects(conn)
    stats    = _global_stats(conn)
    conn.close()
    return templates.TemplateResponse("dashboard.html", {
        "request":   request,
        "projects":  projects,
        "stats":     stats,
        "base_path": settings.get("scanner.base_path", ""),
    })


# ── Projektliste (HTMX-Partial) ───────────────────────────────────────────────

@router.post("/projects/toggle", response_class=HTMLResponse)
async def toggle_project(
    request:    Request,
    path:       str = Form(...),
    name:       str = Form(...),
):
    conn = connection.get_connection()
    row = conn.execute("SELECT * FROM projects WHERE path=?", (path,)).fetchone()
    if row is None:
        with conn:
            conn.execute(
                "INSERT INTO projects (name, path, active) VALUES (?,?,1)",
                (name, path),
            )
    else:
        with conn:
            conn.execute(
                "UPDATE projects SET active=? WHERE id=?",
                (0 if row["active"] else 1, row["id"]),
            )
    projects = _discovered_projects(conn)
    stats    = _global_stats(conn)
    conn.close()
    return templates.TemplateResponse("_dashboard_projects.html", {
        "request":  request,
        "projects": projects,
        "stats":    stats,
    })


# ── Scan starten / Status abfragen ────────────────────────────────────────────

@router.post("/projects/{project_id}/scan", response_class=HTMLResponse)
async def start_scan(request: Request, project_id: int):
    conn = connection.get_connection()
    row  = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    conn.close()
    if not row:
        return HTMLResponse("Projekt nicht gefunden", status_code=404)
    if _scans.get(project_id, {}).get("status") == "running":
        return HTMLResponse(_scan_badge(project_id, "running"))
    _scans[project_id] = {"status": "running", "started_at": _now()}
    threading.Thread(
        target=_run_scan, args=(project_id, row["path"]), daemon=True
    ).start()
    return HTMLResponse(_scan_badge(project_id, "running"))


@router.get("/projects/{project_id}/scan-status", response_class=HTMLResponse)
async def scan_status(project_id: int):
    status = _scans.get(project_id, {}).get("status", "idle")
    return HTMLResponse(_scan_badge(project_id, status))


# ── Ordner-Browser ────────────────────────────────────────────────────────────

@router.get("/browse", response_class=HTMLResponse)
async def browse(
    request:    Request,
    path:       str = Query(...),
    project_id: int = Query(...),
    depth:      int = Query(0),
):
    # READ-ONLY: NAS darf nie verändert werden — nur os.scandir, nur Verzeichnisse
    conn = connection.get_connection()
    ignored = {
        r["path"] for r in conn.execute(
            "SELECT path FROM ignored_paths WHERE project_id=?", (project_id,)
        ).fetchall()
    }
    conn.close()

    subdirs: list[dict] = []
    try:
        with os.scandir(path) as it:       # READ-ONLY: nur lesend
            for entry in sorted(it, key=lambda e: e.name.lower()):
                if not entry.is_dir():     # READ-ONLY: nur Verzeichnisse
                    continue
                subdirs.append({
                    "name":        entry.name,
                    "path":        entry.path,
                    "ignored":     entry.path in ignored,
                    "has_children": _has_subdirs(entry.path),
                })
    except PermissionError:
        pass

    return templates.TemplateResponse("_dashboard_browse.html", {
        "request":      request,
        "subdirs":      subdirs,
        "current_path": path,
        "project_id":   project_id,
        "depth":        depth,
    })


@router.get("/detail", response_class=HTMLResponse)
async def folder_detail(
    request:    Request,
    path:       str = Query(...),
    project_id: int = Query(...),
):
    conn = connection.get_connection()
    is_ignored = conn.execute(
        "SELECT 1 FROM ignored_paths WHERE project_id=? AND path=?",
        (project_id, path),
    ).fetchone() is not None

    indexed = conn.execute("""
        SELECT COUNT(*) FROM document_paths dp
        JOIN documents d ON d.id = dp.document_id
        WHERE d.project_id = ? AND dp.path LIKE ?
    """, (project_id, f"{path}%")).fetchone()[0]
    conn.close()

    file_count = 0
    try:
        # READ-ONLY: NAS darf nie verändert werden
        with os.scandir(path) as it:
            file_count = sum(1 for e in it if e.is_file())
    except PermissionError:
        pass

    return templates.TemplateResponse("_dashboard_detail.html", {
        "request":       request,
        "path":          path,
        "folder_name":   Path(path).name,
        "project_id":    project_id,
        "is_ignored":    is_ignored,
        "indexed_count": indexed,
        "file_count":    file_count,
    })


@router.post("/ignore", response_class=HTMLResponse)
async def ignore_path(
    request:    Request,
    path:       str = Form(...),
    project_id: int = Form(...),
):
    conn = connection.get_connection()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO ignored_paths (project_id, path) VALUES (?,?)",
            (project_id, path),
        )
    conn.close()
    return await folder_detail(request, path=path, project_id=project_id)


@router.post("/unignore", response_class=HTMLResponse)
async def unignore_path(
    request:    Request,
    path:       str = Form(...),
    project_id: int = Form(...),
):
    conn = connection.get_connection()
    with conn:
        conn.execute(
            "DELETE FROM ignored_paths WHERE project_id=? AND path=?",
            (project_id, path),
        )
    conn.close()
    return await folder_detail(request, path=path, project_id=project_id)


# ── Interne Helpers ───────────────────────────────────────────────────────────

def _discovered_projects(conn) -> list[dict]:
    """Scannt base_path eine Ebene tief; READ-ONLY."""
    base = settings.get("scanner.base_path", "")
    if not base or not Path(base).exists():
        return []

    db_by_path = {
        r["path"]: dict(r)
        for r in conn.execute("SELECT * FROM projects").fetchall()
    }

    results: list[dict] = []
    try:
        # READ-ONLY: NAS darf nie verändert werden — nur os.scandir
        with os.scandir(base) as it:
            for entry in sorted(it, key=lambda e: e.name.lower()):
                if not entry.is_dir():
                    continue
                path = entry.path
                db   = db_by_path.get(path)
                if db:
                    count     = conn.execute(
                        "SELECT COUNT(*) FROM documents WHERE project_id=?",
                        (db["id"],),
                    ).fetchone()[0]
                    last_scan = conn.execute(
                        "SELECT MAX(indexed_at) FROM documents WHERE project_id=?",
                        (db["id"],),
                    ).fetchone()[0]
                    results.append({
                        "name":        db["name"],
                        "path":        path,
                        "in_db":       True,
                        "id":          db["id"],
                        "active":      bool(db["active"]),
                        "doc_count":   count,
                        "last_scan":   (last_scan or "")[:10] or None,
                        "scan_status": _scans.get(db["id"], {}).get("status"),
                    })
                else:
                    results.append({
                        "name":        entry.name,
                        "path":        path,
                        "in_db":       False,
                        "id":          None,
                        "active":      False,
                        "doc_count":   0,
                        "last_scan":   None,
                        "scan_status": None,
                    })
    except PermissionError:
        pass
    return results


def _global_stats(conn) -> dict:
    total    = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    active   = conn.execute("SELECT COUNT(*) FROM projects WHERE active=1").fetchone()[0]
    dupes    = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT document_id FROM document_paths
            GROUP BY document_id HAVING COUNT(*) > 1
        )
    """).fetchone()[0]
    last     = conn.execute("SELECT MAX(indexed_at) FROM documents").fetchone()[0]
    return {
        "total":           total,
        "active_projects": active,
        "duplicates":      dupes,
        "last_scan":       (last or "")[:10] or "—",
    }


def _has_subdirs(path: str) -> bool:
    # READ-ONLY: NAS darf nie verändert werden
    try:
        with os.scandir(path) as it:
            return any(e.is_dir() for e in it)
    except PermissionError:
        return False


def _run_scan(project_id: int, path: str):
    try:
        scan_project(project_id, Path(path))
        conn  = connection.get_connection()
        count = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE project_id=?", (project_id,)
        ).fetchone()[0]
        conn.close()
        _scans[project_id] = {"status": "done", "count": count, "finished_at": _now()}
    except Exception as exc:
        _scans[project_id] = {"status": "error", "error": str(exc), "finished_at": _now()}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scan_badge(project_id: int, status: str) -> str:
    if status == "running":
        return (
            f'<span class="scan-badge running" '
            f'hx-get="/dashboard/projects/{project_id}/scan-status" '
            f'hx-trigger="every 3s" hx-swap="outerHTML">Scannt…</span>'
        )
    if status == "done":
        count = _scans.get(project_id, {}).get("count", "?")
        return f'<span class="scan-badge done">✓ {count} Dok.</span>'
    if status == "error":
        return '<span class="scan-badge error">Fehler beim Scan</span>'
    return ""
