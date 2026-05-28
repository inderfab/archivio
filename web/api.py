"""JSON-API."""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import settings
from db import connection
from web.dashboard import _mail_scan, _run_mail_scan, _run_scan, _scans, _now

router = APIRouter(prefix="/api")

_VERSION_FILE = Path(__file__).parent.parent / "VERSION"


@router.get("/version")
async def version():
    v = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "0.0.0"
    return JSONResponse({"version": v})


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


@router.post("/scan/all")
async def scan_all():
    conn     = connection.get_connection()
    projects = conn.execute(
        "SELECT id, path FROM projects WHERE active=1"
    ).fetchall()
    conn.close()

    started = 0
    for p in projects:
        if _scans.get(p["id"], {}).get("status") == "running":
            continue
        _scans[p["id"]] = {"status": "running", "started_at": _now()}
        threading.Thread(
            target=_run_scan, args=(p["id"], p["path"]), daemon=True
        ).start()
        started += 1

    if _mail_scan.get("status") != "running":
        _mail_scan.clear()
        _mail_scan["status"]     = "running"
        _mail_scan["started_at"] = _now()
        threading.Thread(target=_run_mail_scan, daemon=True).start()

    return JSONResponse({"ok": True, "projects_started": started})


@router.post("/update")
async def update_server():
    """git pull + pip install, danach LaunchAgent-Neustart."""
    project_root = Path(__file__).parent.parent

    def _run():
        try:
            subprocess.run(["git", "pull"], cwd=project_root, timeout=60)
            venv_pip = project_root / ".venv" / "bin" / "pip"
            subprocess.run(
                [str(venv_pip), "install", "-q", "-r", "requirements.txt"],
                cwd=project_root, timeout=120,
            )
        except Exception:
            pass
        # LaunchAgent-Neustart: KeepAlive sorgt für automatischen Wiederstart
        try:
            subprocess.run(["launchctl", "stop", "ch.strut.archivio"], timeout=5)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True, "message": "Update läuft, Server startet neu…"})
