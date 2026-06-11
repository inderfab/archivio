"""Dashboard: Projektverwaltung und Ordner-Browser."""
from __future__ import annotations

import json
import logging
import os
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from config import settings
from db import connection
from scanner.walker import scan_project
from web.shared import templates

router = APIRouter(prefix="/dashboard")
log = logging.getLogger(__name__)

# In-Memory Scan-Status  {project_id: {status, count, started_at, ...}}
_scans: dict[int, dict] = {}
# Cancel-Flags {project_id: {"cancel": bool}}
_cancel_flags: dict[int, dict] = {}
# Mail-Scan-Status
_mail_scan: dict = {}


# ── Dashboard-Hauptseite ──────────────────────────────────────────────────────

_DIST = Path(__file__).parent.parent / "dist"
# Fallback: DATA_DIR (Library/Application Support/Archivio/dist) — dort legt Postinstall den ZIP ab
_DIST_DATA = Path.home() / "Library" / "Application Support" / "Archivio" / "dist"
_VERSION_FILE = Path(__file__).parent.parent / "VERSION"


def _helper_info() -> tuple[bool, str]:
    version = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "1.0.0"
    # Alle Suchpfade: Bundle-dist + DATA_DIR-dist
    for dist_dir in (_DIST, _DIST_DATA):
        if (dist_dir / f"archivio-helper-{version}.zip").exists():
            return True, version
    # Fallback: neusten verfügbaren ZIP in beiden Verzeichnissen suchen
    candidates = sorted(
        list(_DIST.glob("archivio-helper-*.zip")) +
        list(_DIST_DATA.glob("archivio-helper-*.zip"))
        if _DIST_DATA.exists() else list(_DIST.glob("archivio-helper-*.zip"))
    )
    if candidates:
        return True, candidates[-1].stem.replace("archivio-helper-", "")
    return False, version


def _server_info() -> tuple[bool, str]:
    version = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "1.0.0"
    zip_path = _DIST / f"archivio-server-{version}.zip"
    return zip_path.exists(), version


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request):
    conn = connection.get_connection()
    connection.init_schema()
    groups = _project_groups(conn)
    stats  = _global_stats(conn)
    conn.close()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "groups":  groups,
        "stats":   stats,
    })


@router.get("/problem-docs", response_class=HTMLResponse)
async def problem_docs(request: Request):
    conn = connection.get_connection()
    docs = _problem_documents(conn)
    conn.close()
    if not docs:
        return HTMLResponse("")
    return templates.TemplateResponse("_problem_docs.html", {
        "request":      request,
        "problem_docs": docs,
    })


@router.get("/download/helper")
async def download_helper():
    _, version = _helper_info()
    fname = f"archivio-helper-{version}.zip"
    for dist_dir in (_DIST, _DIST_DATA):
        zip_path = dist_dir / fname
        if zip_path.exists():
            return FileResponse(zip_path, media_type="application/zip", filename=fname)
    from fastapi.responses import JSONResponse
    return JSONResponse({"error": "Kein Build vorhanden"}, status_code=404)


@router.get("/download/server")
async def download_server():
    _, version = _server_info()
    zip_path = _DIST / f"archivio-server-{version}.zip"
    if not zip_path.exists():
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Kein Build vorhanden"}, status_code=404)
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"archivio-server-{version}.zip",
    )


# ── Projektliste (HTMX-Partial) ───────────────────────────────────────────────

@router.get("/projects/list", response_class=HTMLResponse)
async def projects_list(request: Request):
    """Projektliste neu laden (z.B. nach Abbrechen eines Dialogs)."""
    conn = connection.get_connection()
    groups = _project_groups(conn)
    stats  = _global_stats(conn)
    conn.close()
    return templates.TemplateResponse("_dashboard_projects.html", {
        "request": request, "groups": groups, "stats": stats,
    })


@router.post("/projects/toggle", response_class=HTMLResponse)
async def toggle_project(
    request: Request,
    path:    str = Form(...),
    name:    str = Form(...),
):
    conn = connection.get_connection()
    row  = conn.execute("SELECT * FROM projects WHERE path=?", (path,)).fetchone()
    if row is None:
        # Neu aktivieren
        with conn:
            conn.execute(
                "INSERT INTO projects (name, path, active) VALUES (?,?,1)",
                (name, path),
            )
        _rematch_unassigned_mailboxes(conn)
        groups = _project_groups(conn)
        stats  = _global_stats(conn)
        conn.close()
        return templates.TemplateResponse("_dashboard_projects.html", {
            "request": request, "groups": groups, "stats": stats,
        })
    elif row["active"]:
        # Aktives Projekt deaktivieren → Rückfrage ob aus DB entfernen
        doc_count = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE project_id=?", (row["id"],)
        ).fetchone()[0]
        conn.close()
        return templates.TemplateResponse("_dashboard_project_confirm_remove.html", {
            "request":   request,
            "project_id": row["id"],
            "name":      row["name"],
            "path":      path,
            "doc_count": doc_count,
        })
    else:
        # Wieder aktivieren
        with conn:
            conn.execute("UPDATE projects SET active=1 WHERE id=?", (row["id"],))
        _rematch_unassigned_mailboxes(conn)
        groups = _project_groups(conn)
        stats  = _global_stats(conn)
        conn.close()
        return templates.TemplateResponse("_dashboard_projects.html", {
            "request": request, "groups": groups, "stats": stats,
        })


@router.post("/projects/{project_id}/deactivate", response_class=HTMLResponse)
async def deactivate_project(request: Request, project_id: int):
    """Nur deaktivieren, Daten behalten."""
    conn = connection.get_connection()
    with conn:
        conn.execute("UPDATE projects SET active=0 WHERE id=?", (project_id,))
    _rematch_unassigned_mailboxes(conn)
    groups = _project_groups(conn)
    stats  = _global_stats(conn)
    conn.close()
    return templates.TemplateResponse("_dashboard_projects.html", {
        "request": request, "groups": groups, "stats": stats,
    })


@router.post("/projects/{project_id}/delete", response_class=HTMLResponse)
async def delete_project(request: Request, project_id: int):
    """Projekt und alle Dokumente aus DB entfernen."""
    conn = connection.get_connection()
    with conn:
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    groups = _project_groups(conn)
    stats  = _global_stats(conn)
    conn.close()
    return templates.TemplateResponse("_dashboard_projects.html", {
        "request": request, "groups": groups, "stats": stats,
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
    _scans[project_id] = {
        "status":       "running",
        "phase":        "collecting",
        "project_name": row["name"],
        "total":        0,
        "processed":    0,
        "new":          0,
        "skipped":      0,
        "errors":       0,
        "current_file": "",
        "started_at":   _now(),
    }
    _cancel_flags[project_id] = {"cancel": False}
    threading.Thread(
        target=_run_scan, args=(project_id, row["path"]), daemon=True
    ).start()
    return HTMLResponse(_scan_badge(project_id, "running"))


@router.get("/projects/{project_id}/scan-status", response_class=HTMLResponse)
async def scan_status(project_id: int):
    status = _scans.get(project_id, {}).get("status", "idle")
    return HTMLResponse(_scan_badge(project_id, status))


@router.post("/projects/{project_id}/scan/cancel", response_class=HTMLResponse)
async def cancel_scan(project_id: int):
    flag = _cancel_flags.get(project_id)
    if flag:
        flag["cancel"] = True
    s = _scans.get(project_id, {})
    if s.get("status") == "running":
        s["status"] = "cancelled"
    return HTMLResponse(_scan_badge(project_id, "cancelled"))


@router.get("/projects/{project_id}/scan-progress")
async def scan_progress_json(project_id: int):
    s = _scans.get(project_id, {})
    if not s:
        return JSONResponse({"status": "idle"})
    total     = s.get("total", 0)
    processed = s.get("processed", 0)
    percent   = int(processed / total * 100) if total > 0 else 0
    return JSONResponse({
        "status":       s.get("status", "idle"),
        "phase":        s.get("phase", ""),
        "total":        total,
        "processed":    processed,
        "percent":      percent,
        "new":          s.get("new", 0),
        "skipped":      s.get("skipped", 0),
        "errors":       s.get("errors", 0),
        "current_file": s.get("current_file", ""),
    })


@router.get("/scan-progress-banner", response_class=HTMLResponse)
async def scan_progress_banner(request: Request):
    """Liefert den Fortschritts-Banner für HTMX-Polling. Leer wenn kein Scan aktiv."""
    active = None
    for pid, s in _scans.items():
        status = s.get("status")
        if status == "running":
            active = (pid, s)
            break
        if status in ("done", "error"):
            elapsed = _elapsed_seconds(s.get("finished_at", ""))
            if elapsed < 30:
                active = (pid, s)

    if active is None:
        return HTMLResponse("")

    pid, s    = active
    total     = s.get("total", 0)
    processed = s.get("processed", 0)
    percent   = int(processed / total * 100) if total > 0 else 0
    return templates.TemplateResponse("_scan_progress.html", {
        "request":      request,
        "status":       s.get("status"),
        "phase":        s.get("phase", ""),
        "project_name": s.get("project_name", ""),
        "total":        total,
        "processed":    processed,
        "percent":      percent,
        "new":          s.get("new", 0),
        "skipped":      s.get("skipped", 0),
        "errors":       s.get("errors", 0),
        "current_file": s.get("current_file", ""),
        "error":        s.get("error", ""),
        "project_id":   pid,
    })


def _elapsed_seconds(iso_str: str) -> float:
    try:
        from datetime import datetime, timezone
        finished = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - finished).total_seconds()
    except Exception:
        return 999.0


# ── Mail-Integration ──────────────────────────────────────────────────────────

@router.get("/mail", response_class=HTMLResponse)
async def mail_dashboard(request: Request):
    conn    = connection.get_connection()
    configs = conn.execute("""
        SELECT msc.id, msc.mailbox_name, msc.active, msc.last_scanned_at, msc.mail_count,
               p.name AS project_name, p.id AS project_id
        FROM mail_scan_config msc
        LEFT JOIN projects p ON p.id = msc.project_id
        ORDER BY msc.mailbox_name
    """).fetchall()
    conn.close()
    return templates.TemplateResponse("_dashboard_mail.html", {
        "request":     request,
        "configs":     [dict(r) for r in configs],
        "scan_status": _mail_scan.get("status"),
        "scan_new":    _mail_scan.get("total_new"),
        "scan_error":  _mail_scan.get("error"),
    })


@router.get("/mail/refresh", response_class=HTMLResponse)
async def mail_refresh(request: Request):
    from scanner.mail_scanner import connect_imap, list_mailboxes, match_mailbox_to_project
    try:
        client    = connect_imap()
        mailboxes = list_mailboxes(client)
        try:
            client.logout()
        except Exception:
            pass

        conn = connection.get_connection()
        for mb in mailboxes:
            pid = match_mailbox_to_project(conn, mb)
            conn.execute(
                """INSERT INTO mail_scan_config (mailbox_name, project_id, active)
                   VALUES (?, ?, 0)
                   ON CONFLICT(mailbox_name) DO UPDATE SET project_id=excluded.project_id""",
                (mb, pid),
            )
        conn.commit()
        conn.close()
    except Exception as exc:
        return HTMLResponse(
            f'<div class="search-error" style="padding:12px;">IMAP-Fehler: {exc}</div>'
        )
    return await mail_dashboard(request)


@router.post("/mail/toggle", response_class=HTMLResponse)
async def mail_toggle(
    request:      Request,
    mailbox_name: str = Form(...),
    context:      str = Form(""),
):
    conn = connection.get_connection()
    row  = conn.execute(
        "SELECT active FROM mail_scan_config WHERE mailbox_name=?", (mailbox_name,)
    ).fetchone()
    if row:
        with conn:
            conn.execute(
                "UPDATE mail_scan_config SET active=? WHERE mailbox_name=?",
                (0 if row["active"] else 1, mailbox_name),
            )
    if context == "project":
        groups = _project_groups(conn)
        stats  = _global_stats(conn)
        conn.close()
        return templates.TemplateResponse("_dashboard_projects.html", {
            "request": request, "groups": groups, "stats": stats,
        })
    conn.close()
    return await mail_dashboard(request)


@router.post("/mail/scan", response_class=HTMLResponse)
async def mail_scan_start(request: Request):
    if _mail_scan.get("status") == "running":
        return await mail_dashboard(request)
    _mail_scan.clear()
    _mail_scan["status"]     = "running"
    _mail_scan["started_at"] = _now()
    threading.Thread(target=_run_mail_scan, daemon=True).start()
    return await mail_dashboard(request)


@router.get("/mail/scan-status", response_class=HTMLResponse)
async def mail_scan_status(request: Request):
    return await mail_dashboard(request)


# ── Ordner-Browser ────────────────────────────────────────────────────────────

@router.get("/browse", response_class=HTMLResponse)
async def browse(
    request:    Request,
    path:       str = Query(...),
    project_id: int = Query(...),
    depth:      int = Query(0),
):
    conn = connection.get_connection()
    ignored = {
        r["path"] for r in conn.execute(
            "SELECT path FROM ignored_paths WHERE project_id=?", (project_id,)
        ).fetchall()
    }
    conn.close()

    excluded = {unicodedata.normalize('NFC', f.lower()) for f in settings.get("scanner.excluded_folders", [])}

    subdirs: list[dict] = []
    error_msg: str = ""
    try:
        with os.scandir(path) as it:
            for entry in sorted(it, key=lambda e: e.name.lower()):
                if not entry.is_dir() or entry.name.startswith('.'):
                    continue
                subdirs.append({
                    "name":         entry.name,
                    "path":         entry.path,
                    "ignored":      entry.path in ignored,
                    "excluded":     any(excl in unicodedata.normalize('NFC', entry.name.lower()) for excl in excluded),
                    "has_children": _has_subdirs(entry.path),
                })
    except PermissionError:
        log.warning("Kein Zugriff auf Ordner: %s (macOS Full Disk Access prüfen)", path)
        error_msg = "Kein Zugriff auf diesen Ordner. macOS-Berechtigungen (Datenschutz → Festplattenvollzugriff) prüfen."
    except FileNotFoundError:
        log.warning("Ordner nicht gefunden: %s", path)
        error_msg = f"Ordner nicht gefunden: {path}"
    except OSError as exc:
        log.warning("Fehler beim Lesen von %s: %s", path, exc)
        error_msg = f"Ordner konnte nicht gelesen werden: {exc}"

    return templates.TemplateResponse("_dashboard_browse.html", {
        "request":      request,
        "subdirs":      subdirs,
        "error_msg":    error_msg,
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
    resp = await folder_detail(request, path=path, project_id=project_id)
    resp.headers["HX-Trigger"] = json.dumps({"archivio:browseEntryChanged": {"path": path, "ignored": True}})
    return resp


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
    resp = await folder_detail(request, path=path, project_id=project_id)
    resp.headers["HX-Trigger"] = json.dumps({"archivio:browseEntryChanged": {"path": path, "ignored": False}})
    return resp


def _rematch_unassigned_mailboxes(conn) -> None:
    """Ordnet Postfächer ohne Projekt-Zuordnung neu zu."""
    try:
        from scanner.mail_scanner import match_mailbox_to_project
        rows = conn.execute(
            "SELECT mailbox_name FROM mail_scan_config WHERE project_id IS NULL"
        ).fetchall()
        for row in rows:
            pid = match_mailbox_to_project(conn, row["mailbox_name"])
            if pid:
                with conn:
                    conn.execute(
                        "UPDATE mail_scan_config SET project_id=? WHERE mailbox_name=?",
                        (pid, row["mailbox_name"]),
                    )
    except Exception as exc:
        log.warning("Mailbox-Rematch fehlgeschlagen: %s", exc)


# ── Einstellungen ────────────────────────────────────────────────────────────

def _helper_url_hint(cfg: dict) -> str:
    """URL, die Mitarbeiter im Helper eintragen sollen."""
    import socket
    port = cfg.get("server", {}).get("port", 8000)
    try:
        hostname = socket.gethostname()
        if not hostname.endswith(".local"):
            hostname += ".local"
    except Exception:
        hostname = "localhost"
    return f"http://{hostname}:{port}"


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    saved:   str = Query(default=""),
    restart: str = Query(default=""),
):
    cfg = settings.load_all()
    # Einmal-Migration: altes mail-{} → mail_accounts: [{}]
    if "mail_accounts" not in cfg and "mail" in cfg:
        old = cfg["mail"]
        cfg["mail_accounts"] = [{
            "label":    "Haupt",
            "host":     old.get("host", ""),
            "port":     old.get("port", 993),
            "username": old.get("username", ""),
            "password": old.get("password", ""),
        }]
    available, version = _helper_info()
    return templates.TemplateResponse("settings.html", {
        "request":          request,
        "cfg":              cfg,
        "saved":            bool(saved),
        "restart":          bool(restart),
        "helper_available": available,
        "helper_version":   version,
        "helper_url_hint":  _helper_url_hint(cfg),
    })


@router.post("/settings")
async def settings_save(request: Request):
    form = await request.form()

    old_cfg  = settings.load_all()
    old_host = str(old_cfg.get("server", {}).get("host", ""))
    old_port = str(old_cfg.get("server", {}).get("port", ""))
    old_accounts = old_cfg.get("mail_accounts", old_cfg.get("mail", []))
    if isinstance(old_accounts, dict):
        old_accounts = [old_accounts]

    # Büro
    office_name     = form.get("office_name", "").strip()
    office_language = form.get("office_language", "de")

    # Server
    server_host = form.get("server_host", "127.0.0.1").strip()
    server_port = int(form.get("server_port", "8000") or "8000")
    scan_time   = form.get("scan_time", "").strip()

    # Mail — mehrere Konten
    labels    = form.getlist("mail_label")
    hosts     = form.getlist("mail_host")
    ports     = form.getlist("mail_port")
    usernames = form.getlist("mail_username")
    passwords = form.getlist("mail_password")

    mail_accounts = []
    for i, (lbl, h, po, u, pw) in enumerate(zip(labels, hosts, ports, usernames, passwords)):
        if not h.strip() and not u.strip():
            continue
        if not pw.strip():
            # bestehendes Passwort behalten
            pw = old_accounts[i]["password"] if i < len(old_accounts) else ""
        mail_accounts.append({
            "label":    lbl.strip(),
            "host":     h.strip(),
            "port":     int(po or 993),
            "username": u.strip(),
            "password": pw.strip(),
        })

    # Scanner — base_folders
    bf_labels = form.getlist("base_folder_label")
    bf_paths  = form.getlist("base_folder_path")
    base_folders = [
        {"label": l.strip(), "path": p.strip()}
        for l, p in zip(bf_labels, bf_paths)
        if p.strip()
    ]

    # Scanner — excluded_folders
    excluded_folders = [
        v.strip() for v in form.getlist("excluded_folder") if v.strip()
    ]

    updates = {
        "office": {
            "name":     office_name,
            "language": office_language,
        },
        "server":        {"host": server_host, "port": server_port},
        "scheduler":     {"scan_time": scan_time},
        "mail_accounts": mail_accounts,
        # erstes Konto auch unter mail: {} für Rückwärtskompatibilität
        "mail":          mail_accounts[0] if mail_accounts else {},
        "scanner": {
            "base_folders":     base_folders,
            "excluded_folders": excluded_folders,
        },
    }
    settings.save(updates)

    restart_required = (server_host != old_host or str(server_port) != old_port)
    params = "?saved=1" + ("&restart=1" if restart_required else "")
    return RedirectResponse(f"/dashboard/settings{params}", status_code=303)


@router.post("/settings/test-mail", response_class=HTMLResponse)
async def settings_test_mail(request: Request):
    form     = await request.form()
    host     = form.get("mail_host", "").strip()
    port     = int(form.get("mail_port", "993") or "993")
    username = form.get("mail_username", "").strip()
    password = form.get("mail_password", "").strip()
    if not password:
        # Passwort aus gespeichertem Konto holen (anhand Username-Match)
        for acc in settings.get("mail_accounts") or []:
            if acc.get("username") == username:
                password = acc.get("password", "")
                break
    try:
        import imaplib
        client = imaplib.IMAP4_SSL(host, port)
        client.login(username, password)
        client.logout()
        return HTMLResponse('<span class="test-ok">✓ Verbindung erfolgreich</span>')
    except Exception as exc:
        return HTMLResponse(f'<span class="test-err">✗ {exc}</span>')


# ── Interne Helpers ───────────────────────────────────────────────────────────

def _project_groups(conn) -> list[dict]:
    base_folders = settings.get("scanner.base_folders", [])
    db_by_path   = {
        r["path"]: dict(r)
        for r in conn.execute("SELECT * FROM projects").fetchall()
    }
    groups = []
    for folder in base_folders:
        label    = folder.get("label", "")
        base     = folder.get("path", "")
        projects = _discovered_projects_for_base(conn, base, db_by_path)
        groups.append({"label": label, "path": base, "projects": projects})
    return groups


def _discovered_projects_for_base(conn, base: str, db_by_path: dict) -> list[dict]:
    if not base or not Path(base).exists():
        return []
    results: list[dict] = []
    try:
        with os.scandir(base) as it:
            for entry in sorted(it, key=lambda e: e.name.lower()):
                if not entry.is_dir() or entry.name.startswith('.'):
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
                    mailboxes = conn.execute(
                        "SELECT * FROM mail_scan_config WHERE project_id=?",
                        (db["id"],),
                    ).fetchall()
                    results.append({
                        "name":        db["name"],
                        "path":        path,
                        "in_db":       True,
                        "id":          db["id"],
                        "active":      bool(db["active"]),
                        "doc_count":   count,
                        "last_scan":   _fmt_iso_date(last_scan),
                        "scan_status": _scans.get(db["id"], {}).get("status"),
                        "mailboxes":   [dict(m) for m in mailboxes],
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
                        "mailboxes":   [],
                    })
    except PermissionError:
        pass
    return results


def _problem_documents(conn) -> list[dict]:
    """Gibt alle nicht vollständig verarbeiteten Dokumente zurück mit wahrscheinlichem Grund."""
    result = []

    # 1. error / unsupported
    rows = conn.execute("""
        SELECT d.filename, d.extension, d.filesize, dp.path, d.extraction_status
        FROM documents d
        LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        WHERE d.extraction_status IN ('error', 'unsupported')
        ORDER BY d.extension, d.filename
    """).fetchall()

    for row in rows:
        result.append({
            "filename": row["filename"],
            "reason":   _error_reason(row["extension"], row["path"]),
            "category": "error",
        })

    # 2. ok aber keine Chunks — bildbasierte PDFs (Pläne, Scans)
    empty_rows = conn.execute("""
        SELECT d.filename, d.filesize
        FROM documents d
        WHERE d.extraction_status = 'ok'
          AND NOT EXISTS (SELECT 1 FROM document_chunks dc WHERE dc.document_id = d.id)
        ORDER BY d.filename
    """).fetchall()

    _PLAN_KEYWORDS = ("grundriss", "lageplan", "situation", "aushub", "schnitt",
                      "ansicht", "plan", "fassade", "detail")
    for row in empty_rows:
        nl = row["filename"].lower()
        if any(kw in nl for kw in _PLAN_KEYWORDS):
            reason = "Kein Textinhalt – Architekturplan"
        elif row["filesize"] > 5 * 1024 * 1024:
            reason = "Kein Textinhalt – Bilddatei / gescanntes PDF"
        else:
            reason = "Kein Textinhalt – Bildbasiertes PDF"
        result.append({
            "filename": row["filename"],
            "reason":   reason,
            "category": "no_text",
        })

    return result


def _error_reason(extension: str, path: str | None) -> str:
    if extension == ".xlsx":
        return "Timeout – Datei zu komplex für Extraktion"
    if extension == ".pdf":
        return "PDF fehlerhaft, passwortgeschützt oder unlesbar"
    return "Datei fehlerhaft oder Format nicht unterstützt"


def _global_stats(conn) -> dict:
    total  = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM projects WHERE active=1").fetchone()[0]
    dupes  = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT document_id FROM document_paths
            GROUP BY document_id HAVING COUNT(*) > 1
        )
    """).fetchone()[0]
    last   = conn.execute("SELECT MAX(indexed_at) FROM documents").fetchone()[0]
    return {
        "total":           total,
        "active_projects": active,
        "duplicates":      dupes,
        "last_scan":       _fmt_iso_date(last) or "—",
    }


def _has_subdirs(path: str) -> bool:
    try:
        with os.scandir(path) as it:
            return any(e.is_dir() and not e.name.startswith('.') for e in it)
    except PermissionError:
        return False


def _run_mail_scan():
    from scanner.mail_scanner import connect_imap, scan_mailbox
    try:
        client = connect_imap()
        conn   = connection.get_connection()
        active = conn.execute(
            "SELECT * FROM mail_scan_config WHERE active=1 AND project_id IS NOT NULL"
        ).fetchall()
        conn.close()

        total_new = 0
        for row in active:
            try:
                stats      = scan_mailbox(client, row["mailbox_name"], row["project_id"],
                                          progress=_mail_scan)
                total_new += stats["new"]
            except Exception as exc:
                log.error("Postfach '%s' fehlgeschlagen: %s", row["mailbox_name"], exc)

        try:
            client.logout()
        except Exception:
            pass

        _mail_scan["status"]      = "done"
        _mail_scan["total_new"]   = total_new
        _mail_scan["finished_at"] = _now()
    except Exception as exc:
        _mail_scan["status"] = "error"
        _mail_scan["error"]  = str(exc)


def _run_scan(project_id: int, path: str):
    progress = _scans[project_id]
    cancel_flag = _cancel_flags.get(project_id, {})
    try:
        scan_project(project_id, Path(path), progress=progress, cancel_flag=cancel_flag)
        if progress.get("phase") == "error":
            progress.update({"status": "error", "finished_at": _now(),
                             "error": progress.get("error", "Pfad nicht zugänglich")})
            return
        if cancel_flag.get("cancel"):
            progress.update({"status": "cancelled", "finished_at": _now()})
            return
        conn  = connection.get_connection()
        count = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE project_id=?", (project_id,)
        ).fetchone()[0]
        conn.close()
        progress.update({"status": "done", "count": count, "finished_at": _now()})
    except Exception as exc:
        progress.update({"status": "error", "error": str(exc), "finished_at": _now()})


def _fmt_iso_date(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        y, m, d = iso[:10].split("-")
        return f"{d}.{m}.{y}"
    except ValueError:
        return iso[:10]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scan_badge(project_id: int, status: str) -> str:
    if status == "running":
        processed = _scans.get(project_id, {}).get("processed", 0)
        total     = _scans.get(project_id, {}).get("total", 0)
        label     = f"Scannt… {processed}/{total}" if total else "Scannt…"
        return (
            f'<span class="scan-running-wrap" '
            f'hx-get="/dashboard/projects/{project_id}/scan-status" '
            f'hx-trigger="every 3s" hx-swap="outerHTML">'
            f'<span class="scan-badge running">{label}</span>'
            f'<button class="btn btn-cancel" style="margin-left:6px;" '
            f'hx-post="/dashboard/projects/{project_id}/scan/cancel" '
            f'hx-swap="outerHTML" hx-target="closest .scan-running-wrap">Abbrechen</button>'
            f'</span>'
        )
    if status == "cancelled":
        processed = _scans.get(project_id, {}).get("processed", 0)
        return f'<span class="scan-badge error">⏹ Abgebrochen ({processed} verarbeitet)</span>'
    if status == "done":
        count = _scans.get(project_id, {}).get("count", "?")
        if count == 0 or count == "0":
            return (
                f'<span class="scan-badge error" title="0 Dokumente gefunden — '
                f'Dateitypen, Pfad und macOS-Zugriffsrechte prüfen (Vollzugriff auf Festplatte).">'
                f'⚠ 0 Dok. gefunden</span>'
            )
        return f'<span class="scan-badge done">✓ {count} Dok.</span>'
    if status == "error":
        error = _scans.get(project_id, {}).get("error", "")
        title = f' title="{error}"' if error else ""
        return f'<span class="scan-badge error"{title}>Fehler beim Scan</span>'
    return ""
