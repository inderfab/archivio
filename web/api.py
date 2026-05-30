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


_update_log: list[str] = []


@router.get("/update/log")
async def update_log():
    return JSONResponse({"log": _update_log[-50:]})


_GITHUB_REPO = "inderfab/archivio"
_in_bundle   = bool(os.environ.get("ARCHIVIO_DATA_DIR"))


@router.post("/update")
async def update_server():
    """Bundle-Modus: GitHub-Release herunterladen + App ersetzen + Neustart.
       Dev-Modus: git pull + pip install + LaunchAgent-Neustart."""
    _update_log.clear()

    def _log(msg: str):
        _update_log.append(msg)
        import logging
        logging.getLogger(__name__).info(msg)

    if _in_bundle:
        def _run_bundle():
            import requests as _req
            _log("Prüfe GitHub Releases…")
            try:
                resp = _req.get(
                    f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest",
                    timeout=10,
                    headers={"Accept": "application/vnd.github+json"},
                )
                if resp.status_code != 200:
                    _log(f"GitHub: HTTP {resp.status_code}")
                    return
                data = resp.json()
                remote_ver = data.get("tag_name", "").lstrip("v")
                current = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "0.0.0"
                _log(f"Lokal: {current}  →  GitHub: {remote_ver or '(kein Release)'}")

                download_url = next(
                    (a["browser_download_url"] for a in data.get("assets", [])
                     if "archivio-server" in a["name"] and a["name"].endswith(".zip")),
                    None,
                )
                if not download_url:
                    _log("Kein ZIP-Asset im Release — bitte .pkg manuell installieren.")
                    return

                _log("Lade Update herunter…")
                zip_resp = _req.get(download_url, timeout=180, stream=True)
                zip_path = Path("/tmp/archivio-server-update.zip")
                with open(zip_path, "wb") as f:
                    for chunk in zip_resp.iter_content(65536):
                        f.write(chunk)
                _log("ZIP heruntergeladen")

                # Bundle: api.py liegt in Contents/Resources/web/
                code_root  = Path(__file__).parent.parent   # Contents/Resources
                app_bundle = code_root.parent.parent        # Archivio Server.app
                dest_dir   = app_bundle.parent              # /Applications/
                _log(f"Extrahiere nach {dest_dir}")
                r = subprocess.run(
                    ["ditto", "-x", "-k", str(zip_path), str(dest_dir)],
                    capture_output=True, text=True,
                )
                if r.returncode != 0:
                    _log(f"ditto Fehler: {r.stderr}")
                    return
                zip_path.unlink(missing_ok=True)
                _log(f"Update nach {dest_dir} extrahiert")

                # App neu starten
                restart = Path("/tmp/archivio-restart.sh")
                restart.write_text(
                    "#!/bin/bash\nsleep 4\n"
                    "osascript -e 'quit app \"Archivio Server\"' 2>/dev/null || true\n"
                    "sleep 2\nopen -a 'Archivio Server'\n"
                )
                restart.chmod(0o755)
                subprocess.Popen(["bash", str(restart)], start_new_session=True)
                _log("Server wird neu gestartet…")
            except Exception as e:
                _log(f"Update-Fehler: {e}")

        threading.Thread(target=_run_bundle, daemon=True).start()

    else:
        project_root = Path(__file__).parent.parent

        def _run_dev():
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

        threading.Thread(target=_run_dev, daemon=True).start()

    return JSONResponse({"ok": True, "message": "Update läuft, Server startet neu…"})
