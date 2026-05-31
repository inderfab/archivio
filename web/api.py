"""JSON-API."""
from __future__ import annotations

import os
import subprocess
import threading
import zipfile
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import settings
from db import connection
from web.dashboard import _mail_scan, _run_mail_scan, _run_scan, _scans, _cancel_flags, _now

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


_ollama_install_state: dict = {"running": False, "done": False, "error": "", "log": []}


@router.post("/ai/install-ollama")
async def install_ollama():
    if _ollama_install_state.get("running"):
        return JSONResponse({"ok": False, "message": "Installation läuft bereits"})

    def _run():
        _ollama_install_state.update({"running": True, "done": False, "error": "", "log": []})
        brew = next(
            (p for p in ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"] if Path(p).exists()),
            None
        )
        if not brew:
            _ollama_install_state.update({
                "running": False,
                "error": "Homebrew nicht gefunden. Bitte Homebrew zuerst installieren (brew.sh)."
            })
            return
        try:
            proc = subprocess.Popen(
                [brew, "install", "ollama"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            for line in proc.stdout:
                _ollama_install_state["log"].append(line.rstrip())
            proc.wait()
            if proc.returncode == 0:
                _ollama_install_state.update({"running": False, "done": True})
            else:
                _ollama_install_state.update({
                    "running": False,
                    "error": f"Installation fehlgeschlagen (Exit {proc.returncode})"
                })
        except Exception as e:
            _ollama_install_state.update({"running": False, "error": str(e)})

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True})


@router.get("/ai/install-ollama/status")
async def install_ollama_status():
    return JSONResponse(_ollama_install_state)


_GITHUB_REPO = "inderfab/archivio"


@router.get("/update/check")
async def update_check():
    """Prüft ob eine neue Version auf GitHub verfügbar ist."""
    import requests as _req
    current = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "0.0.0"
    try:
        resp = _req.get(
            f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest",
            timeout=10,
            headers={"Accept": "application/vnd.github+json"},
        )
        if resp.status_code != 200:
            return JSONResponse({"current": current, "update_available": False})
        remote_ver = resp.json().get("tag_name", "").lstrip("v")
        update_available = bool(remote_ver) and remote_ver != current
        return JSONResponse({
            "current": current,
            "latest": remote_ver,
            "update_available": update_available,
            "download_url": "https://inderfab.github.io/archivio/index.html",
        })
    except Exception:
        return JSONResponse({"current": current, "update_available": False})
