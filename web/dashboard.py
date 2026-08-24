"""Dashboard: Projektverwaltung und Ordner-Browser."""
from __future__ import annotations

import gc
import json
import logging
import os
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

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
# Lösch-Status {project_id: "running"|"done"|"error"}
_deletions: dict[int, str] = {}
# Globale Scan-Sperre: max. 1 Worker-Prozess gleichzeitig
_scan_lock        = threading.Semaphore(1)
_embed_thread_lock = threading.Lock()   # max. 1 Embedding-Thread gleichzeitig
_fts_opt_lock      = threading.Lock()   # max. 1 FTS-Optimize-Thread gleichzeitig

_EMBED_BATCH_DOCS  = 5    # Dokumente pro Batch, dann GC + Pause
_EMBED_BATCH_PAUSE = 3.0  # Sekunden Pause zwischen Batches (normal)
_EMBED_RAM_PAUSE   = 90   # Sekunden Pause wenn RAM-Grenze erreicht
# Prozess-RSS-Obergrenze fuer das Embedding — MUSS unter der Watchdog-Grenze
# (server_app.py _SERVER_RAM_LIMIT_GB = 20 GB) liegen, sonst treibt das Embedding
# den Server in den Neustart, bevor es sich selbst drosselt (frueherer Neustart-Loop).
_EMBED_MAX_RSS_GB  = 15.0


def _embedding_ram_ok() -> bool:
    """False wenn das Embedding pausieren soll — misst den EIGENEN Prozess-RSS
    (nicht system-weites RAM%, das die 20-GB-Prozessgrenze nie rechtzeitig sieht)."""
    try:
        import psutil, os as _os
        rss_gb = psutil.Process(_os.getpid()).memory_info().rss / (1024 ** 3)
        if rss_gb > _EMBED_MAX_RSS_GB:
            return False
        return psutil.virtual_memory().percent < 85
    except Exception:
        return True


# ── Dashboard-Hauptseite ──────────────────────────────────────────────────────

_DIST = Path(__file__).parent.parent / "dist"
# Fallback: DATA_DIR (Library/Application Support/Archivio/dist) — dort legt Postinstall den ZIP ab
_DIST_DATA = Path.home() / "Library" / "Application Support" / "Archivio" / "dist"
_VERSION_FILE = Path(__file__).parent.parent / "VERSION"
# Von scripts/build_server_app.sh ins Bundle geschrieben (Contents/Resources/HELPER_VERSION) --
# Server und Helper sind unabhängig versioniert, die Server-VERSION sagt nichts über die
# tatsächlich mitgelieferte Helper-Version aus.
_HELPER_VERSION_FILE = Path(__file__).parent.parent / "HELPER_VERSION"
# Dev-Checkout ohne Bundle (kein HELPER_VERSION vorhanden)
_HELPER_VERSION_FILE_DEV = Path(__file__).parent.parent / "helper" / "VERSION"


def _helper_info() -> tuple[bool, str]:
    """Bevorzugt .pkg (systemweite Installation nach /Applications -- siehe
    helper/build.sh) vor .zip (aeltere Auslieferung, manuelles Draganddrop war
    Ursache fuer 'Helper bei anderen Benutzern unsichtbar')."""
    if _HELPER_VERSION_FILE.exists():
        version = _HELPER_VERSION_FILE.read_text().strip()
    elif _HELPER_VERSION_FILE_DEV.exists():
        version = _HELPER_VERSION_FILE_DEV.read_text().strip()
    else:
        version = "1.0.0"
    # Alle Suchpfade: Bundle-dist + DATA_DIR-dist
    for dist_dir in (_DIST, _DIST_DATA):
        if (dist_dir / f"archivio-helper-{version}.pkg").exists():
            return True, version
        if (dist_dir / f"archivio-helper-{version}.zip").exists():
            return True, version
    # Fallback: neuesten verfügbaren Build (pkg oder zip) in beiden Verzeichnissen
    # suchen -- nach Version sortiert, NICHT alphabetisch ("archivio-helper-3.1.9.zip"
    # > "...3.1.11.zip" als String, weil '9' > '1' -- lieferte lange die falsche,
    # ältere Version aus).
    candidates = (
        list(_DIST.glob("archivio-helper-*.pkg")) +
        list(_DIST.glob("archivio-helper-*.zip")) +
        (list(_DIST_DATA.glob("archivio-helper-*.pkg")) if _DIST_DATA.exists() else []) +
        (list(_DIST_DATA.glob("archivio-helper-*.zip")) if _DIST_DATA.exists() else [])
    )
    if candidates:
        from packaging.version import InvalidVersion, Version

        def _parsed(p: Path) -> Version:
            try:
                return Version(p.stem.replace("archivio-helper-", ""))
            except InvalidVersion:
                return Version("0")

        best = max(candidates, key=_parsed)
        return True, best.stem.replace("archivio-helper-", "")
    return False, version


def _server_info() -> tuple[bool, str]:
    version = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "1.0.0"
    zip_path = _DIST / f"archivio-server-{version}.zip"
    return zip_path.exists(), version


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request):
    from scanner.embedder import is_ollama_installed
    conn = connection.get_connection()
    connection.init_schema()
    groups   = _project_groups(conn)
    stats    = _global_stats(conn)
    configs  = conn.execute("""
        SELECT msc.id, msc.mailbox_name, msc.active, msc.last_scanned_at, msc.mail_count,
               p.name AS project_name, p.id AS project_id
        FROM mail_scan_config msc
        LEFT JOIN projects p ON p.id = msc.project_id
        ORDER BY msc.mailbox_name
    """).fetchall()
    projects = conn.execute(
        "SELECT id, name FROM projects WHERE active=1 ORDER BY name"
    ).fetchall()
    conn.close()
    return templates.TemplateResponse("dashboard.html", {
        "request":      request,
        "groups":       groups,
        "stats":        stats,
        "configs":      [dict(r) for r in configs],
        "projects":     [dict(r) for r in projects],
        "scan_status":  _mail_scan.get("status"),
        "scan_new":     _mail_scan.get("total_new"),
        "scan_error":   _mail_scan.get("error"),
        "scan_detail":  _mail_scan.get("detail"),
        "scan_warning": _mail_scan.get("warning"),
        "ollama_missing": not is_ollama_installed(),
    })


@router.get("/problem-docs", response_class=HTMLResponse)
async def problem_docs(request: Request):
    conn = connection.get_connection()
    docs = _problem_documents(conn)
    conn.close()
    if not docs:
        return HTMLResponse('<div id="problem-docs-container"></div>')
    return templates.TemplateResponse("_problem_docs.html", {
        "request":      request,
        "problem_docs": docs,
        "retry_done":   0,
    })


@router.post("/retry-errors", response_class=HTMLResponse)
async def retry_errors(request: Request):
    """Setzt alle error-Dokumente auf 'pending' — beim nächsten Scan neu verarbeitet."""
    conn = connection.get_connection()
    with conn:
        placeholders = ",".join("?" * len(_TEXT_EXTRACTABLE))
        count = conn.execute(
            f"UPDATE documents SET extraction_status = 'pending'"
            f" WHERE extraction_status = 'error' AND extension IN ({placeholders})",
            _TEXT_EXTRACTABLE,
        ).rowcount
    docs = _problem_documents(conn)
    conn.close()
    if not docs:
        return HTMLResponse('<details id="problem-docs-section"></details>')
    return templates.TemplateResponse("_problem_docs.html", {
        "request":      request,
        "problem_docs": docs,
        "retry_done":   count,
    })


@router.get("/download/helper")
async def download_helper():
    """Liefert bevorzugt das Helper-PKG aus (installiert systemweit nach /Applications,
    sichtbar fuer alle Benutzer des Macs -- siehe helper/build.sh), sonst die aeltere
    ZIP als Fallback. Die server_url wird NICHT hier vorbelegt (patchte frueher
    config.json innerhalb der bereits signierten+notarisierten Datei -- das bricht die
    Codesignatur, macOS zeigt dann "beschaedigt". Der Helper sucht den Server
    stattdessen selbst per mDNS (menubar_bridge.discover_servers),
    siehe helper/archivio_helper.py)."""
    _, version = _helper_info()
    for ext, media_type in ((".pkg", "application/octet-stream"), (".zip", "application/zip")):
        fname = f"archivio-helper-{version}{ext}"
        for dist_dir in (_DIST, _DIST_DATA):
            p = dist_dir / fname
            if p.exists():
                return FileResponse(p, media_type=media_type, filename=fname)
    return JSONResponse({"error": "Kein Build vorhanden"}, status_code=404)


@router.get("/download/server")
async def download_server():
    _, version = _server_info()
    zip_path = _DIST / f"archivio-server-{version}.zip"
    if not zip_path.exists():
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


def _delete_project_bg(project_id: int) -> None:
    """Löscht Projekt + alle Dokumente im Hintergrund-Thread."""
    try:
        conn = connection.get_connection()
        with conn:
            # Mails der verknüpften Postfächer explizit löschen: ihr project_id kann
            # NULL/abweichend sein (Postfach wurde evtl. vor der Verknüpfung gescannt),
            # wird also NICHT vom Projekt-CASCADE erfasst. Verknüpfung über mailbox_name.
            mailbox_names = [r[0] for r in conn.execute(
                "SELECT mailbox_name FROM mail_scan_config WHERE project_id=?", (project_id,)
            ).fetchall()]
            mail_docs = 0
            for mb in mailbox_names:
                ids = [r[0] for r in conn.execute(
                    "SELECT document_id FROM mails WHERE mailbox_name=?", (mb,)
                ).fetchall()]
                for did in ids:
                    conn.execute("DELETE FROM documents WHERE id=?", (did,))
                mail_docs += len(ids)

            # Anzahl Projekt-Dateien (für Log); werden per CASCADE mitgelöscht
            file_docs = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE project_id=?", (project_id,)
            ).fetchone()[0]

            # mail_scan_config hat kein ON DELETE CASCADE — Postfach mitlöschen
            conn.execute("DELETE FROM mail_scan_config WHERE project_id=?", (project_id,))
            # Projekt löschen → CASCADE entfernt Projekt-Dokumente (Dateien),
            # Trigger documents_fts_doc_delete räumt die FTS-Einträge auf.
            conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
        conn.close()
        _deletions[project_id] = "done"
        log.info("Projekt %s aus DB gelöscht (%d Dateien, %d Mails)",
                 project_id, file_docs, mail_docs)
    except Exception as exc:
        log.error("Fehler beim Löschen von Projekt %s: %s", project_id, exc)
        _deletions[project_id] = "error:" + str(exc)


def _delete_loading_html(project_id: int) -> str:
    return (
        f'<div style="padding:12px 16px; color:var(--text-3); font-size:13px;'
        f' border:1px solid var(--border); border-radius:8px; margin-bottom:8px;"'
        f' hx-get="/dashboard/projects/{project_id}/delete-status"'
        f' hx-trigger="every 2s"'
        f' hx-target="#project-list"'
        f' hx-swap="innerHTML">'
        f'⟳ Wird aus Datenbank entfernt…</div>'
    )


@router.post("/projects/{project_id}/delete", response_class=HTMLResponse)
async def delete_project(project_id: int):
    """Startet Löschung im Hintergrund, gibt sofort Loading-State zurück."""
    if _deletions.get(project_id) != "running":
        _deletions[project_id] = "running"
        threading.Thread(
            target=_delete_project_bg, args=(project_id,), daemon=True
        ).start()
    return HTMLResponse(_delete_loading_html(project_id))


@router.get("/projects/{project_id}/delete-status", response_class=HTMLResponse)
async def delete_project_status(request: Request, project_id: int):
    """Polling-Endpunkt: liefert Loading-State, Fehler oder fertige Projektliste."""
    status = _deletions.get(project_id, "done")
    if status == "running":
        return HTMLResponse(_delete_loading_html(project_id))
    if status.startswith("error:"):
        err = status[6:]
        _deletions.pop(project_id, None)
        return HTMLResponse(
            f'<div style="padding:12px 16px; color:#b91c1c; font-size:13px;'
            f' border:1px solid #fca5a5; border-radius:8px; margin-bottom:8px;">'
            f'⚠ Fehler beim Löschen: {err}<br>'
            f'<small>Falls ein Scan läuft, bitte diesen zuerst stoppen und erneut versuchen.</small>'
            f'</div>'
        )
    _deletions.pop(project_id, None)
    conn = connection.get_connection()
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
        "project_root": row["path"],
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
    # Ganze Zelle zurückgeben (Ziel #scan-cell-…), damit der Scan-Button bleibt
    return HTMLResponse(_scan_cell(project_id, "cancelled"))


@router.get("/projects/{project_id}/scan-progress")
async def scan_progress_json(project_id: int):
    s = _scans.get(project_id, {})
    if not s:
        return JSONResponse({"status": "idle"})
    total     = s.get("total", 0)
    processed = s.get("processed", 0)
    percent   = s.get("percent", int(processed / total * 100) if total > 0 else 0)
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


@router.get("/stats", response_class=HTMLResponse)
async def dashboard_stats(request: Request):
    conn = connection.get_connection()
    stats = _global_stats(conn)
    conn.close()
    return templates.TemplateResponse("_dashboard_stats.html", {
        "request": request, "stats": stats,
    })


@router.get("/scan-progress-banner", response_class=HTMLResponse)
async def scan_progress_banner(request: Request):
    """Liefert den Fortschritts-Banner für HTMX-Polling. Leer wenn kein Scan aktiv."""
    # Bei "Alle scannen" stehen viele Projekte gleichzeitig auf status="running"
    # (die meisten warten nur auf den _scan_lock, total=0/processed=0). Das Projekt
    # mit ECHTEM Fortschritt (das den Lock hat und gerade tatsächlich verarbeitet)
    # hat Vorrang vor bloss wartenden — sonst zeigt der Banner zufällig irgendein
    # wartendes Projekt bei "Ordner wird durchsucht…", während in der Liste darunter
    # ein ANDERES Projekt echten Fortschritt zeigt (verwirrender Widerspruch).
    active = None
    queued_only = None
    recent_finished = None
    for pid, s in _scans.items():
        status = s.get("status")
        if status == "running":
            has_progress = s.get("total", 0) > 0 or s.get("processed", 0) > 0
            if has_progress:
                active = (pid, s)
                break
            if queued_only is None:
                queued_only = (pid, s)
        elif status in ("done", "error"):
            elapsed = _elapsed_seconds(s.get("finished_at", ""))
            if elapsed < 8:
                recent_finished = (pid, s)
    if active is None:
        active = queued_only or recent_finished

    if active is None:
        # Kein Projekt-Scan → ggf. Mail-Scan im selben grossen Banner zeigen
        ms = _mail_scan
        ms_status = ms.get("status")
        ms_active = ms_status == "running" or (
            ms_status in ("done", "error") and _elapsed_seconds(ms.get("finished_at", "")) < 8
        )
        if ms_active:
            resp = templates.TemplateResponse("_scan_progress.html", {
                "request":        request,
                "status":         ms_status,
                "phase":          "",
                "project_name":   "Mail-Scan",
                "project_root":   "Mail-Scan",
                "current_dir":    ("📬 " + ms.get("current_mailbox", "")) if ms.get("current_mailbox") else "📬 Mail-Scan",
                "total":          ms.get("total", 0),
                "processed":      ms.get("processed", 0),
                "new":            ms.get("new", ms.get("total_new", 0)) or 0,
                "skipped":        ms.get("skipped", 0),
                "listed":         0,
                "errors":         ms.get("errors", 0),
                "current_file":   ms.get("current_mailbox", ""),
                "current_folder": "",
                "error":          ms.get("error", ""),
                "project_id":     None,   # kein Abbrechen-Button für Mail
                "unit":           "Mails",
                "searching_label": "Postfächer werden vorbereitet…",
            })
            if ms_status in ("done", "error"):
                import json as _json
                resp.headers["HX-Trigger"] = _json.dumps({"archivio:scanComplete": True})
            return resp
        return HTMLResponse("")

    pid, s    = active
    total     = s.get("total", 0)
    processed = s.get("processed", 0)
    # Percent wird vom Scanner vorberechnet (Ordner-gewichtet); Fallback für alte Einträge
    percent   = s.get("percent", int(processed / total * 100) if total > 0 else 0)
    resp = templates.TemplateResponse("_scan_progress.html", {
        "request":      request,
        "status":       s.get("status"),
        "phase":        s.get("phase", ""),
        "project_name": s.get("project_name", ""),
        "project_root": s.get("project_root", ""),
        "total":        total,
        "processed":    processed,
        "new":          s.get("new", 0),
        "skipped":      s.get("skipped", 0),
        "listed":       s.get("listed", 0),
        "errors":       s.get("errors", 0),
        "current_file":   s.get("current_file", ""),
        "current_folder": s.get("current_folder", ""),
        "current_dir":    s.get("current_dir", ""),
        "error":          s.get("error", ""),
        "project_id":     pid,
    })
    if s.get("status") in ("done", "error"):
        import json as _json
        resp.headers["HX-Trigger"] = _json.dumps({"archivio:scanComplete": True})
    return resp


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
    projects = conn.execute(
        "SELECT id, name FROM projects WHERE active=1 ORDER BY name"
    ).fetchall()
    conn.close()
    cfg_list = []
    for r in configs:
        d = dict(r)
        _lbl, _cls = _scan_freshness(d.get("last_scanned_at"))
        d["scan_fresh_label"] = _lbl
        d["scan_fresh_class"] = _cls
        cfg_list.append(d)
    return templates.TemplateResponse("_dashboard_mail.html", {
        "request":      request,
        "configs":      cfg_list,
        "projects":     [dict(r) for r in projects],
        "scan_status":  _mail_scan.get("status"),
        "scan_new":     _mail_scan.get("total_new"),
        "scan_error":   _mail_scan.get("error"),
        "scan_detail":  _mail_scan.get("detail"),
        "scan_warning": _mail_scan.get("warning"),
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
                   ON CONFLICT(mailbox_name) DO UPDATE
                     SET project_id = COALESCE(mail_scan_config.project_id, excluded.project_id)""",
                (mb, pid),
            )
        conn.commit()
        conn.close()
    except Exception as exc:
        return HTMLResponse(
            f'<div class="search-error" style="padding:12px;">IMAP-Fehler: {exc}</div>'
        )
    return await mail_dashboard(request)


async def _mail_section_response(request: Request, conn, context: str):
    """Liefert je nach Kontext die aktualisierte Projektliste oder den Mail-Bereich."""
    if context == "project":
        groups = _project_groups(conn)
        stats  = _global_stats(conn)
        conn.close()
        return templates.TemplateResponse("_dashboard_projects.html", {
            "request": request, "groups": groups, "stats": stats,
        })
    conn.close()
    return await mail_dashboard(request)


@router.post("/mail/toggle", response_class=HTMLResponse)
async def mail_toggle(
    request:      Request,
    mailbox_name: str = Form(...),
    context:      str = Form(""),
):
    conn = connection.get_connection()
    row  = conn.execute(
        "SELECT active, mail_count FROM mail_scan_config WHERE mailbox_name=?", (mailbox_name,)
    ).fetchone()
    if not row:
        return await _mail_section_response(request, conn, context)

    # Deaktivieren → Rückfrage, ob die Mails aus der DB gelöscht werden sollen
    if row["active"]:
        conn.close()
        return templates.TemplateResponse("_dashboard_mail_confirm_remove.html", {
            "request":      request,
            "mailbox_name": mailbox_name,
            "mail_count":   row["mail_count"] or 0,
            "context":      context,
        })

    # Aktivieren → einfach einschalten
    with conn:
        conn.execute(
            "UPDATE mail_scan_config SET active=1 WHERE mailbox_name=?", (mailbox_name,)
        )
    return await _mail_section_response(request, conn, context)


@router.post("/mail/deactivate", response_class=HTMLResponse)
async def mail_deactivate(
    request:      Request,
    mailbox_name: str = Form(...),
    context:      str = Form(""),
):
    """Postfach deaktivieren, Mails behalten."""
    conn = connection.get_connection()
    with conn:
        conn.execute(
            "UPDATE mail_scan_config SET active=0 WHERE mailbox_name=?", (mailbox_name,)
        )
    return await _mail_section_response(request, conn, context)


@router.post("/mail/delete", response_class=HTMLResponse)
async def mail_delete(
    request:      Request,
    mailbox_name: str = Form(...),
    context:      str = Form(""),
):
    """Postfach deaktivieren UND alle seine Mails aus der DB löschen."""
    conn = connection.get_connection()
    with conn:
        # documents-Delete cascadet auf mails/content/chunks; Trigger räumt FTS.
        conn.execute(
            "DELETE FROM documents WHERE id IN "
            "(SELECT document_id FROM mails WHERE mailbox_name=?)", (mailbox_name,)
        )
        conn.execute(
            "UPDATE mail_scan_config SET active=0, mail_count=0 WHERE mailbox_name=?",
            (mailbox_name,)
        )
    log.info("Postfach '%s' deaktiviert und Mails gelöscht", mailbox_name)
    return await _mail_section_response(request, conn, context)


@router.post("/mail/assign-project", response_class=HTMLResponse)
async def mail_assign_project(
    request:      Request,
    mailbox_name: str = Form(...),
    project_id:   str = Form(""),
):
    conn = connection.get_connection()
    if project_id == "new":
        # Postfach als eigenes Projekt anlegen
        virtual_path = f"mailbox:{mailbox_name}"
        display_name = mailbox_name.split("/")[-1]  # letzter Teil bei "acc/INBOX"
        with conn:
            existing = conn.execute(
                "SELECT id FROM projects WHERE path=?", (virtual_path,)
            ).fetchone()
            if existing:
                pid = existing["id"]
            else:
                pid = conn.execute(
                    "INSERT INTO projects (name, path, active) VALUES (?, ?, 1)",
                    (display_name, virtual_path),
                ).lastrowid
        conn.execute(
            "UPDATE mail_scan_config SET project_id=?, active=1 WHERE mailbox_name=?",
            (pid, mailbox_name),
        )
        conn.commit()
    else:
        pid = int(project_id) if project_id else None
        with conn:
            conn.execute(
                "UPDATE mail_scan_config SET project_id=? WHERE mailbox_name=?",
                (pid, mailbox_name),
            )
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


@router.post("/mail/scan-one", response_class=HTMLResponse)
async def mail_scan_one(
    request:      Request,
    mailbox_name: str = Form(...),
    context:      str = Form(""),
):
    """Scannt genau EIN Postfach (per-Postfach 'Jetzt scannen'-Button)."""
    if _mail_scan.get("status") != "running":
        _mail_scan.clear()
        _mail_scan["status"]     = "running"
        _mail_scan["started_at"] = _now()
        threading.Thread(target=_run_mail_scan,
                         kwargs={"only_mailbox": mailbox_name}, daemon=True).start()
    return await _mail_section_response(request, connection.get_connection(), context)


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
    project_paths = {
        r["path"] for r in conn.execute(
            "SELECT path FROM projects WHERE active=1"
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
                ep = entry.path
                subdirs.append({
                    "name":           entry.name,
                    "path":           ep,
                    "ignored":        ep in ignored,
                    "excluded":       any(excl in unicodedata.normalize('NFC', entry.name.lower()) for excl in excluded),
                    "has_children":   _has_subdirs(ep),
                    "is_project":     ep in project_paths,
                    "has_subproject": any(p.startswith(ep + "/") for p in project_paths),
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

    sub_project = conn.execute(
        "SELECT id FROM projects WHERE path=? AND active=1", (path,)
    ).fetchone()
    conn.close()

    file_count = 0
    try:
        with os.scandir(path) as it:
            file_count = sum(1 for e in it if e.is_file())
    except PermissionError:
        pass

    return templates.TemplateResponse("_dashboard_detail.html", {
        "request":        request,
        "path":           path,
        "folder_name":    Path(path).name,
        "project_id":     project_id,
        "is_ignored":     is_ignored,
        "indexed_count":  indexed,
        "file_count":     file_count,
        "is_sub_project": sub_project is not None,
        "sub_project_id": sub_project["id"] if sub_project else None,
    })


@router.post("/make-project", response_class=HTMLResponse)
async def make_project(
    request:    Request,
    path:       str = Form(...),
    project_id: int = Form(...),
):
    """Aktiviert einen Ordner als eigenständiges Projekt."""
    name = Path(path).name
    conn = connection.get_connection()
    existing = conn.execute("SELECT id FROM projects WHERE path=?", (path,)).fetchone()
    with conn:
        if existing:
            conn.execute("UPDATE projects SET active=1 WHERE path=?", (path,))
        else:
            conn.execute(
                "INSERT INTO projects (name, path, active) VALUES (?,?,1)", (name, path)
            )
    conn.close()
    resp = await folder_detail(request, path=path, project_id=project_id)
    resp.headers["HX-Trigger"] = json.dumps({
        "archivio:projectListChanged": True,
        "archivio:browseProjectChanged": {"path": path, "is_project": True},
    })
    return resp


@router.post("/remove-project", response_class=HTMLResponse)
async def remove_project_from_path(
    request:    Request,
    path:       str = Form(...),
    project_id: int = Form(...),
):
    """Deaktiviert das Sub-Projekt für diesen Ordner."""
    conn = connection.get_connection()
    with conn:
        conn.execute("UPDATE projects SET active=0 WHERE path=?", (path,))
    conn.close()
    resp = await folder_detail(request, path=path, project_id=project_id)
    resp.headers["HX-Trigger"] = json.dumps({
        "archivio:projectListChanged": True,
        "archivio:browseProjectChanged": {"path": path, "is_project": False},
    })
    return resp


@router.post("/ignore-level", response_class=HTMLResponse)
async def ignore_level(
    request:    Request,
    path:       str = Form(...),
    project_id: int = Form(...),
):
    """Ignoriert alle Ordner auf der gleichen Ebene wie 'path' (Geschwister-Ordner)."""
    parent = str(Path(path).parent)
    conn = connection.get_connection()
    # Erst alle Einträge sammeln, dann jeden einzeln einfügen (eigene Transaktion pro Insert)
    siblings: list[str] = []
    try:
        with os.scandir(parent) as it:
            siblings = [e.path for e in it if e.is_dir() and not e.name.startswith('.')]
    except (PermissionError, OSError):
        pass
    for sibling_path in siblings:
        try:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO ignored_paths (project_id, path) VALUES (?,?)",
                    (project_id, sibling_path),
                )
        except Exception as exc:
            log.warning("ignore_level: Insert fehlgeschlagen %s: %s", sibling_path, exc)
    conn.close()
    resp = await folder_detail(request, path=path, project_id=project_id)
    resp.headers["HX-Trigger"] = json.dumps({
        "archivio:ignoredLevel": {"parent_path": parent}
    })
    return resp


@router.post("/unignore-level", response_class=HTMLResponse)
async def unignore_level(
    request:    Request,
    path:       str = Form(...),
    project_id: int = Form(...),
):
    """Hebt die Ignorierung aller Geschwister-Ordner von 'path' auf."""
    parent = str(Path(path).parent)
    conn = connection.get_connection()
    siblings: list[str] = []
    try:
        with os.scandir(parent) as it:
            siblings = [e.path for e in it if e.is_dir() and not e.name.startswith('.')]
    except (PermissionError, OSError):
        pass
    for sibling_path in siblings:
        try:
            with conn:
                conn.execute(
                    "DELETE FROM ignored_paths WHERE project_id=? AND path=?",
                    (project_id, sibling_path),
                )
        except Exception as exc:
            log.warning("unignore_level: Delete fehlgeschlagen %s: %s", sibling_path, exc)
    conn.close()
    resp = await folder_detail(request, path=path, project_id=project_id)
    resp.headers["HX-Trigger"] = json.dumps({
        "archivio:ignoredLevel": {"parent_path": parent, "ignored": False}
    })
    return resp


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


# ── Batch-Aktionen (Mehrfachauswahl) ─────────────────────────────────────────

class _BatchPaths(BaseModel):
    project_id: int
    paths: list[str]


@router.post("/batch-ignore")
async def batch_ignore(data: _BatchPaths):
    conn = connection.get_connection()
    with conn:
        for path in data.paths:
            conn.execute(
                "INSERT OR IGNORE INTO ignored_paths (project_id, path) VALUES (?,?)",
                (data.project_id, path),
            )
    conn.close()
    return JSONResponse({"ok": True, "count": len(data.paths)})


@router.post("/batch-unignore")
async def batch_unignore(data: _BatchPaths):
    conn = connection.get_connection()
    with conn:
        for path in data.paths:
            conn.execute(
                "DELETE FROM ignored_paths WHERE project_id=? AND path=?",
                (data.project_id, path),
            )
    conn.close()
    return JSONResponse({"ok": True, "count": len(data.paths)})


@router.post("/batch-make-projects")
async def batch_make_projects(data: _BatchPaths):
    conn = connection.get_connection()
    created = 0
    with conn:
        for path in data.paths:
            name = Path(path).name
            if not conn.execute("SELECT 1 FROM projects WHERE path=?", (path,)).fetchone():
                conn.execute(
                    "INSERT INTO projects (name, path, active) VALUES (?,?,1)",
                    (name, path),
                )
                created += 1
    conn.close()
    return JSONResponse({"ok": True, "created": created})


# ── Unterordner als eigene Projekte ──────────────────────────────────────────

def _load_subfolders(conn, parent_project_id: int, parent_path: str) -> list[dict]:
    """Unmittelbare Unterordner eines Projekts mit ihrem Projekt-Status."""
    db_by_path = {
        r["path"]: dict(r)
        for r in conn.execute("SELECT * FROM projects").fetchall()
    }
    results = []
    try:
        with os.scandir(parent_path) as it:
            for entry in sorted(it, key=lambda e: e.name.lower()):
                if not entry.is_dir() or entry.name.startswith('.'):
                    continue
                path = entry.path
                db   = db_by_path.get(path)
                if db and db["active"]:
                    doc_count = conn.execute(
                        "SELECT COUNT(*) FROM documents WHERE project_id=?", (db["id"],)
                    ).fetchone()[0]
                    last_scan = conn.execute(
                        "SELECT MAX(indexed_at) FROM documents WHERE project_id=?", (db["id"],)
                    ).fetchone()[0]
                    results.append({
                        "name":        db["name"],
                        "path":        path,
                        "is_project":  True,
                        "project_id":  db["id"],
                        "doc_count":   doc_count,
                        "last_scan":   _fmt_iso_date(last_scan),
                        "scan_status": _scans.get(db["id"], {}).get("status"),
                    })
                else:
                    results.append({
                        "name":        entry.name,
                        "path":        path,
                        "is_project":  False,
                        "project_id":  db["id"] if db else None,
                        "doc_count":   0,
                        "last_scan":   None,
                        "scan_status": None,
                    })
    except PermissionError:
        pass
    return results


@router.get("/projects/{project_id}/subfolders", response_class=HTMLResponse)
async def project_subfolders(request: Request, project_id: int):
    conn = connection.get_connection()
    row  = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        conn.close()
        return HTMLResponse("Projekt nicht gefunden", status_code=404)
    subfolders = _load_subfolders(conn, project_id, row["path"])
    conn.close()
    return templates.TemplateResponse("_dashboard_subfolders.html", {
        "request":        request,
        "project_id":     project_id,
        "subfolders":     subfolders,
    })


@router.post("/projects/{project_id}/subfolders/activate", response_class=HTMLResponse)
async def activate_subfolder(
    request:    Request,
    project_id: int,
    path:       str = Form(...),
):
    conn   = connection.get_connection()
    parent = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not parent:
        conn.close()
        return HTMLResponse("Projekt nicht gefunden", status_code=404)
    name     = Path(path).name
    existing = conn.execute("SELECT id FROM projects WHERE path=?", (path,)).fetchone()
    with conn:
        if existing:
            conn.execute("UPDATE projects SET active=1 WHERE path=?", (path,))
        else:
            conn.execute(
                "INSERT INTO projects (name, path, active) VALUES (?,?,1)", (name, path)
            )
    subfolders = _load_subfolders(conn, project_id, parent["path"])
    conn.close()
    return templates.TemplateResponse("_dashboard_subfolders.html", {
        "request":    request,
        "project_id": project_id,
        "subfolders": subfolders,
    })


@router.post("/projects/{project_id}/subfolders/deactivate", response_class=HTMLResponse)
async def deactivate_subfolder(
    request:       Request,
    project_id:    int,
    subproject_id: int = Form(...),
):
    conn   = connection.get_connection()
    parent = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not parent:
        conn.close()
        return HTMLResponse("Projekt nicht gefunden", status_code=404)
    with conn:
        conn.execute("UPDATE projects SET active=0 WHERE id=?", (subproject_id,))
    subfolders = _load_subfolders(conn, project_id, parent["path"])
    conn.close()
    return templates.TemplateResponse("_dashboard_subfolders.html", {
        "request":    request,
        "project_id": project_id,
        "subfolders": subfolders,
    })


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
    # Schneller FDA-Test: Desktop lesbar?
    import os as _os
    _desktop = Path(_os.environ.get("HOME", str(Path.home()))) / "Desktop"
    try:
        list(_os.scandir(str(_desktop)))
        fda_missing = False
    except PermissionError:
        fda_missing = True
    except FileNotFoundError:
        fda_missing = False
    return templates.TemplateResponse("settings.html", {
        "request":          request,
        "cfg":              cfg,
        "saved":            bool(saved),
        "restart":          bool(restart),
        "helper_available": available,
        "helper_version":   version,
        "helper_url_hint":  _helper_url_hint(cfg),
        "fda_missing":      fda_missing,
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
    num_workers = max(1, min(4, int(form.get("num_workers", "1") or "1")))

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

    # Rubrica — nur "enabled" hier gesetzt; db_path bleibt (falls manuell in config.yaml
    # gesetzt) unangetastet, da settings.save() pro Sektion tief mergt statt zu ersetzen.
    rubrica_enabled = form.get("rubrica_enabled") == "1"

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
            "num_workers":      num_workers,
        },
        "rubrica": {"enabled": rubrica_enabled},
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
                    _last_iso = db["last_scanned_at"] if "last_scanned_at" in db.keys() else None
                    _fresh_label, _fresh_class = _scan_freshness(_last_iso)
                    results.append({
                        "name":            db["name"],
                        "path":            path,
                        "in_db":           True,
                        "id":              db["id"],
                        "active":          bool(db["active"]),
                        "doc_count":       count,
                        "last_scan":       _fmt_iso_date(last_scan),
                        "last_scanned":    _fmt_iso_datetime(_last_iso),
                        "scan_fresh_label": _fresh_label,
                        "scan_fresh_class": _fresh_class,
                        "scan_status":     _scans.get(db["id"], {}).get("status"),
                        "mailboxes":       [dict(m) for m in mailboxes],
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


_TEXT_EXTRACTABLE = (
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".xlsm",
    ".rtf", ".txt", ".csv", ".eml", ".msg", ".pptx", ".ppt",
)

def _problem_documents(conn) -> list[dict]:
    """Gibt Dokumente zurück bei denen die Textextraktion unerwartet scheiterte.
    Nur Formate die Text enthalten sollten (PDF, Word, Mail …) — Bilder und
    CAD-Dateien werden bewusst nicht ausgelesen und zählen nicht als Fehler."""
    result = []

    # Nur Text-Formate mit echtem Extraktionsfehler
    placeholders = ",".join("?" * len(_TEXT_EXTRACTABLE))
    rows = conn.execute(f"""
        SELECT d.filename, d.extension, d.filesize, dp.path, d.extraction_status
        FROM documents d
        LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        WHERE d.extraction_status = 'error'
          AND d.extension IN ({placeholders})
        ORDER BY d.extension, d.filename
    """, _TEXT_EXTRACTABLE).fetchall()

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


def _run_mail_scan(only_mailbox: str | None = None):
    """Scannt aktive Postfächer. only_mailbox: nur dieses eine (für den
    per-Postfach 'Jetzt scannen'-Button). Reihenfolge stale-first (am längsten
    nicht gescannte zuerst) — wie bei den Projekten."""
    from scanner.mail_scanner import connect_imap, scan_mailbox
    try:
        client = connect_imap()
        conn   = connection.get_connection()
        if only_mailbox:
            active = conn.execute(
                "SELECT * FROM mail_scan_config WHERE mailbox_name=?", (only_mailbox,)
            ).fetchall()
        else:
            # stale-first: NULL (nie gescannt) zuerst, dann älteste
            active = conn.execute(
                "SELECT * FROM mail_scan_config WHERE active=1 ORDER BY last_scanned_at ASC"
            ).fetchall()
        conn.close()

        if not active:
            _mail_scan["status"]      = "done"
            _mail_scan["total_new"]   = 0
            _mail_scan["finished_at"] = _now()
            _mail_scan["warning"]     = "Keine aktiven Postfächer — bitte Postfach aktivieren."
            _mail_scan.pop("detail", None)
            _mail_scan.pop("current_mailbox", None)
            return

        total_new = skipped_total = errors_total = 0
        mailbox_details = []
        for row in active:
            if _mail_scan.get("cancel"):
                _mail_scan["status"]      = "cancelled"
                _mail_scan["finished_at"] = _now()
                _mail_scan.pop("current_mailbox", None)
                try:
                    client.logout()
                except Exception:
                    pass
                return
            _mail_scan["current_mailbox"] = row["mailbox_name"]
            if row["project_id"] is None:
                log.warning("Postfach '%s' hat kein Projekt — übersprungen", row["mailbox_name"])
                mailbox_details.append(f"{row['mailbox_name']}: kein Projekt")
                continue
            try:
                stats = scan_mailbox(client, row["mailbox_name"], row["project_id"],
                                     progress=_mail_scan)
                total_new     += stats["new"]
                skipped_total += stats["skipped"]
                errors_total  += stats["errors"]
                # Live-Zähler fürs Banner
                _mail_scan["new"]     = total_new
                _mail_scan["skipped"] = skipped_total
                _mail_scan["errors"]  = errors_total
                log.info("Postfach '%s': %s", row["mailbox_name"], stats)
                short = row["mailbox_name"].split("/")[-1]
                mailbox_details.append(
                    f"{short}: {stats['new']} neu, {stats['skipped']} übersprungen"
                    + (f", {stats['errors']} Fehler" if stats["errors"] else "")
                )
            except Exception as exc:
                log.error("Postfach '%s' fehlgeschlagen: %s", row["mailbox_name"], exc)
                mailbox_details.append(f"{row['mailbox_name']}: Fehler — {exc}")

        try:
            client.logout()
        except Exception:
            pass

        _mail_scan["status"]      = "done"
        _mail_scan["total_new"]   = total_new
        _mail_scan["finished_at"] = _now()
        _mail_scan["detail"]      = " · ".join(mailbox_details) if mailbox_details else None
        _mail_scan.pop("warning", None)
        _mail_scan.pop("current_mailbox", None)
    except Exception as exc:
        _mail_scan["status"] = "error"
        _mail_scan["error"]  = str(exc)
        _mail_scan.pop("current_mailbox", None)


def _scan_project_mailboxes(project_id: int) -> None:
    """Scannt die mit dem Projekt verknüpften AKTIVEN Postfächer (IMAP).
    Fehler werden geloggt, beeinträchtigen den Datei-Scan-Status nicht."""
    conn = connection.get_connection()
    mbs  = conn.execute(
        "SELECT mailbox_name FROM mail_scan_config WHERE project_id=? AND active=1",
        (project_id,),
    ).fetchall()
    conn.close()
    if not mbs:
        return
    from scanner.mail_scanner import connect_imap, scan_mailbox
    client = connect_imap()
    try:
        for r in mbs:
            try:
                stats = scan_mailbox(client, r["mailbox_name"], project_id)
                log.info("Projekt %s Postfach '%s': %s", project_id, r["mailbox_name"], stats)
            except Exception as exc:
                log.warning("Postfach '%s' (Projekt %s) fehlgeschlagen: %s",
                            r["mailbox_name"], project_id, exc)
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _run_scan(project_id: int, path: str, scan_mail: bool = True):
    progress = _scans[project_id]
    cancel_flag = _cancel_flags.get(project_id, {})

    # Mailbox-Projekte: Mail-Scan starten statt Datei-Scan
    if path.startswith("mailbox:"):
        if _mail_scan.get("status") != "running":
            _mail_scan.clear()
            _mail_scan["status"]     = "running"
            _mail_scan["started_at"] = _now()
            threading.Thread(target=_run_mail_scan, daemon=True).start()
        progress.update({"status": "done", "count": 0, "finished_at": _now()})
        return

    # Warten bis kein anderer Scan läuft (1 Worker-Prozess gleichzeitig)
    if not _scan_lock.acquire(blocking=False):
        progress["phase"] = "queued"
        _scan_lock.acquire()  # blockiert bis vorheriger Scan fertig
    if cancel_flag.get("cancel"):
        progress.update({"status": "cancelled", "finished_at": _now()})
        _scan_lock.release()
        return

    try:
        scan_project(project_id, Path(path), progress=progress, cancel_flag=cancel_flag)
        if progress.get("phase") == "error":
            progress.update({"status": "error", "finished_at": _now(),
                             "error": progress.get("error", "Pfad nicht zugänglich")})
            return
        if cancel_flag.get("cancel"):
            progress.update({"status": "cancelled", "finished_at": _now()})
            return
        # Verknüpfte Postfächer als Teil des Projekt-Scans mitscannen (Einzel-Scan).
        # Bei "Alle scannen" übernimmt der globale Mail-Scan (scan_mail=False),
        # damit Postfächer nicht doppelt gescannt werden.
        if scan_mail:
            progress["phase"] = "mail"
            try:
                _scan_project_mailboxes(project_id)
            except Exception as exc:
                log.warning("Mail-Scan für Projekt %s übersprungen: %s", project_id, exc)
        # Status sofort setzen — Banner verschwindet ohne auf den DB-Count zu warten
        progress.update({"status": "done", "finished_at": _now()})
        try:
            conn  = connection.get_connection()
            count = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE project_id=?", (project_id,)
            ).fetchone()[0]
            # Zeitpunkt des Scans persistent festhalten — auch bei Skip-only-Scans,
            # bei denen sich MAX(indexed_at) nicht ändert.
            with conn:
                conn.execute(
                    "UPDATE projects SET last_scanned_at=? WHERE id=?",
                    (progress["finished_at"], project_id),
                )
            conn.close()
            progress["count"] = count
        except Exception:
            pass
        # FTS-Optimize koordiniert anstoßen (läuft erst wenn kein Scan mehr aktiv)
        threading.Thread(target=_run_fts_optimize, daemon=True).start()
        # Embedding nach dem Scan automatisch starten (falls Ollama läuft)
        threading.Thread(target=_run_post_scan_embedding, daemon=True).start()
    except Exception as exc:
        progress.update({"status": "error", "error": str(exc), "finished_at": _now()})
    finally:
        _scan_lock.release()


def _run_fts_optimize():
    """Stösst FTS5-optimize an — koaleszierend und serialisiert mit Scans.

    Mehrere Scan-Abschlüsse (z.B. bei "Alle scannen") lösen dies aus, aber nur
    EIN Optimize läuft gleichzeitig (_fts_opt_lock). Es wartet via _scan_lock bis
    kein Scan aktiv ist, sodass es nie mit Inserts eines laufenden Scans
    konkurriert (sonst: database is locked → verlorene Dokumente). Während des
    Wartens laufende Scans haben so Vorrang; das Optimize läuft, wenn es ruhig ist.
    """
    if not _fts_opt_lock.acquire(blocking=False):
        return  # bereits eingeplant/aktiv — ein Lauf genügt für alle Änderungen
    try:
        from scanner.walker import optimize_fts
        # Auf den GANZEN Batch warten, nicht nur auf eine Lock-Lücke zwischen zwei
        # Projekten. Bei "Alle scannen" stehen noch Projekte in der Warteschlange
        # (status 'running') und der Mail-Scan läuft parallel. Würde optimize hier den
        # _scan_lock zwischen zwei Projekten grapschen, blockierte es das nächste
        # wartende Projekt (erscheint als "hängt in Vorbereitung") und konkurriert mit
        # den Mail-Inserts um die FTS ("database is locked"). Also warten bis wirklich
        # nichts mehr läuft, DANN einmal optimieren.
        while _any_scan_active():
            time.sleep(2)
        _scan_lock.acquire()   # jetzt unbestritten
        try:
            optimize_fts()
        finally:
            _scan_lock.release()
    except Exception as exc:
        log.debug("FTS-Optimize übersprungen: %s", exc)
    finally:
        _fts_opt_lock.release()


def _any_scan_active() -> bool:
    """True wenn irgendein Projekt-Scan (laufend ODER in der Warteschlange) oder
    ein Mail-Scan aktiv ist. Damit wartet das Embedding, bis der GANZE
    'Alle scannen'-Batch durch ist — nicht nur der aktuell laufende Scan."""
    if any(s.get("status") == "running" for s in _scans.values()):
        return True
    if _mail_scan.get("status") == "running":
        return True
    return False


def _run_post_scan_embedding():
    """Embeddings in kleinen Batches berechnen.
    Nur ein Thread gleichzeitig — bei scan_all startet jedes Projekt einen Thread,
    aber alle ausser dem ersten kehren sofort zurück. Der laufende Thread holt
    automatisch alle offenen Chunks, egal von welchem Projekt."""
    if not _embed_thread_lock.acquire(blocking=False):
        return  # bereits aktiv — laufende Instanz verarbeitet alle Chunks
    try:
        from scanner.embedder import is_ollama_running, embed_document_chunks

        while True:
            # Erst wenn der GANZE Scan-Batch (inkl. Warteschlange + Mails) fertig
            # ist — Embedding ist RAM-intensiv und darf den Scan nicht ausbremsen.
            if _any_scan_active():
                log.debug("Embedding wartet — Scan-Batch aktiv")
                time.sleep(30)
                continue

            if not is_ollama_running():
                break

            # Nächste N Dokumente ohne Embedding holen (frisch jedes Mal)
            conn = connection.get_connection()
            try:
                doc_ids = [r[0] for r in conn.execute(
                    "SELECT DISTINCT document_id FROM document_chunks "
                    "WHERE embedding IS NULL LIMIT ?",
                    (_EMBED_BATCH_DOCS,)
                ).fetchall()]
            finally:
                conn.close()

            if not doc_ids:
                log.debug("Embedding abgeschlossen — keine offenen Chunks mehr")
                break

            # Batch einbetten
            conn = connection.get_connection()
            try:
                for doc_id in doc_ids:
                    try:
                        embed_document_chunks(conn, doc_id)
                    except Exception as e:
                        log.debug("Embedding doc %s: %s", doc_id, e)
            finally:
                conn.close()

            # Speicher explizit freigeben
            gc.collect()

            # RAM-Check: bei > 80% Auslastung Pause einlegen
            if not _embedding_ram_ok():
                log.info("Embedding-Pause: RAM > 80%% — warte %ds", _EMBED_RAM_PAUSE)
                time.sleep(_EMBED_RAM_PAUSE)
            else:
                time.sleep(_EMBED_BATCH_PAUSE)

    except Exception as e:
        log.debug("Post-scan Embedding fehlgeschlagen: %s", e)
    finally:
        _embed_thread_lock.release()


def _fmt_iso_date(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        y, m, d = iso[:10].split("-")
        return f"{d}.{m}.{y}"
    except ValueError:
        return iso[:10]


def _fmt_iso_datetime(iso: str | None) -> str | None:
    """Wie _fmt_iso_date, aber mit Uhrzeit (lokal). Für 'zuletzt gescannt'."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return _fmt_iso_date(iso)


_SCAN_FRESH_DAYS = 2  # bis zu 2 Tage gilt als "frisch" (grün)


def _scan_freshness(iso: str | None) -> tuple[str | None, str]:
    """Gibt (Label, CSS-Klasse) für den Scan-Status zurück.
    Grün wenn kürzlich gescannt, sonst amber mit Altersangabe."""
    if not iso:
        return (None, "")
    try:
        dt       = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except Exception:
        return ("✓ gescannt", "done")
    if age_days < _SCAN_FRESH_DAYS:
        return ("✓ gescannt", "done")
    days = max(1, int(age_days))
    return (f"gescannt vor {days} Tg.", "warn")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scan_cell(project_id: int, status: str) -> str:
    """Rendert die komplette Scan-Zelle eines Projekts (Status-Badge + Aktion) mit
    stabiler ID. Alle Aktionen (Scan starten, Abbrechen, Polling) ersetzen die
    ganze Zelle per outerHTML — so gibt es nie doppelte Badges, und der
    Scan-Button ist in jedem Zustand (ausser 'läuft') verfügbar."""
    cid  = f"scan-cell-{project_id}"

    if status == "running":
        scan      = _scans.get(project_id, {})
        processed = scan.get("processed", 0)
        total     = scan.get("total", 0)
        folder    = scan.get("current_folder", "")
        progress  = f" {processed}/{total}" if total else ""
        label     = f"Scan: {folder}{progress}" if folder else f"Scannt…{progress}"
        return (
            f'<span class="scan-cell" id="{cid}" '
            f'hx-get="/dashboard/projects/{project_id}/scan-status" '
            f'hx-trigger="every 3s" hx-swap="outerHTML">'
            f'<span class="scan-badge running">{label}</span>'
            f'<button class="btn btn-cancel" style="margin-left:6px;" '
            f'hx-post="/dashboard/projects/{project_id}/scan/cancel" '
            f'hx-target="#{cid}" hx-swap="outerHTML">Abbrechen</button>'
            f'</span>'
        )

    # last_scanned_at für Button-Label + Freshness aus DB (überlebt Neustart)
    try:
        conn = connection.get_connection()
        row  = conn.execute(
            "SELECT last_scanned_at FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        conn.close()
        last = row["last_scanned_at"] if row else None
    except Exception:
        last = None

    scan_btn = (
        f'<form hx-post="/dashboard/projects/{project_id}/scan" '
        f'hx-target="#{cid}" hx-swap="outerHTML" style="display:contents;">'
        f'<button type="submit" class="btn {"btn" if last else "btn-primary"}">'
        f'{"Neu scannen" if last else "Jetzt scannen"}</button></form>'
    )

    if status == "cancelled":
        processed = _scans.get(project_id, {}).get("processed", 0)
        badge = (f'<span class="scan-badge error" style="margin-right:8px;">'
                 f'⏹ Abgebrochen ({processed} verarbeitet)</span>')
    elif status == "done":
        count = _scans.get(project_id, {}).get("count", "?")
        if count == 0 or count == "0":
            badge = ('<span class="scan-badge error" style="margin-right:8px;" '
                     'title="0 Dokumente gefunden — Dateitypen, Pfad und macOS-Zugriffsrechte '
                     'prüfen (Vollzugriff auf Festplatte).">⚠ 0 Dok. gefunden</span>')
        else:
            badge = f'<span class="scan-badge done" style="margin-right:8px;">✓ {count} Dok.</span>'
    elif status == "error":
        error = _scans.get(project_id, {}).get("error", "")
        title = f' title="{error}"' if error else ""
        badge = f'<span class="scan-badge error"{title} style="margin-right:8px;">Fehler beim Scan</span>'
    else:
        # idle: durable Freshness-Badge (altersabhängig eingefärbt), sonst nichts
        label, cls = _scan_freshness(last)
        if label:
            when  = _fmt_iso_datetime(last)
            badge = (f'<span class="scan-badge {cls}" title="Zuletzt gescannt: {when}" '
                     f'style="margin-right:8px;">{label}</span>')
        else:
            badge = ""

    return f'<span class="scan-cell" id="{cid}">{badge}{scan_btn}</span>'


# Rückwärtskompatibler Alias — überall wo bisher _scan_badge genutzt wurde
def _scan_badge(project_id: int, status: str) -> str:
    return _scan_cell(project_id, status)
