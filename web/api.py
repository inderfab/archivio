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


_update_log: list[str] = []


@router.get("/update/log")
async def update_log():
    return JSONResponse({"log": _update_log[-50:]})


@router.post("/update")
async def update_server():
    """git pull + pip install, danach LaunchAgent-Neustart."""
    project_root = Path(__file__).parent.parent
    _update_log.clear()

    def _log(msg: str):
        _update_log.append(msg)

    def _run():
        _log("git pull starten…")
        try:
            r = subprocess.run(
                ["git", "pull"], cwd=project_root, timeout=60,
                capture_output=True, text=True,
            )
            _log(f"git pull stdout: {r.stdout.strip()}")
            if r.stderr.strip():
                _log(f"git pull stderr: {r.stderr.strip()}")
            _log(f"git pull returncode: {r.returncode}")
        except Exception as e:
            _log(f"git pull Fehler: {e}")

        _log("pip install starten…")
        try:
            venv_pip = project_root / ".venv" / "bin" / "pip"
            r = subprocess.run(
                [str(venv_pip), "install", "-q", "-r", "requirements.txt"],
                cwd=project_root, timeout=120,
                capture_output=True, text=True,
            )
            _log(f"pip returncode: {r.returncode}")
            if r.stderr.strip():
                _log(f"pip stderr: {r.stderr.strip()[:300]}")
        except Exception as e:
            _log(f"pip Fehler: {e}")

        try:
            subprocess.run(["bash", "helper/build.sh"], cwd=project_root, timeout=120)
        except Exception:
            pass

        _log("LaunchAgent stoppen…")
        for label in ("io.archivio.server", "ch.strut.archivio"):
            try:
                r = subprocess.run(
                    ["launchctl", "stop", label], timeout=5,
                    capture_output=True, text=True,
                )
                _log(f"launchctl stop {label}: rc={r.returncode}")
                if r.returncode == 0:
                    break
            except Exception as e:
                _log(f"launchctl Fehler ({label}): {e}")

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True, "message": "Update läuft, Server startet neu…"})
