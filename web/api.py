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


_rechunk_state: dict = {
    "running": False, "done": 0, "total": 0,
    "fixed": 0, "skipped": 0, "error": "",
    "failed_docs": [],  # [{id, filename, reason}]
}

DOC_TIMEOUT = 60  # Sekunden pro Dokument


@router.post("/ai/rechunk")
async def ai_rechunk():
    """Re-chunked Nicht-PDF-Dokumente die einen einzigen zu-grossen Chunk haben."""
    if _rechunk_state.get("running"):
        return JSONResponse({"ok": False, "message": "Läuft bereits"})

    def _rechunk_one(doc_id: int, parts: list[str]) -> str | None:
        """Führt DELETE+INSERT in eigener Connection aus. Gibt Fehlermeldung zurück oder None."""
        try:
            c = connection.get_connection()
            c.execute("PRAGMA busy_timeout = 5000")
            with c:
                c.execute("DELETE FROM document_chunks WHERE document_id = ?", (doc_id,))
                c.executemany(
                    "INSERT INTO document_chunks (document_id, chunk_index, page_number, content) VALUES (?,?,?,?)",
                    [(doc_id, i, None, part) for i, part in enumerate(parts)]
                )
            c.close()
            return None
        except Exception as e:
            return str(e)

    def _run():
        from scanner.extractors import split_text_into_chunks
        _rechunk_state.update({
            "running": True, "done": 0, "total": 0,
            "fixed": 0, "skipped": 0, "error": "", "failed_docs": [],
        })

        # Kandidaten: erst alle nicht-PDF Docs holen (kleine Tabelle),
        # dann pro Doc einen schnellen Index-Lookup auf document_chunks.
        # Vermeidet Full Table Scan der riesigen chunks-Tabelle.
        try:
            conn = connection.get_connection()
            conn.execute("PRAGMA busy_timeout = 10000")
            doc_rows = conn.execute("""
                SELECT id, filename FROM documents
                WHERE extraction_status = 'ok'
                AND extension NOT IN ('.pdf', '')
                AND source_type = 'filesystem'
            """).fetchall()
            conn.close()
        except Exception as e:
            _rechunk_state.update({"running": False, "error": str(e)})
            return

        # Pro Doc prüfen ob genau 1 Chunk > 1000 Zeichen (Index-Lookup)
        candidates = []
        for doc in doc_rows:
            try:
                c = connection.get_connection()
                c.execute("PRAGMA busy_timeout = 5000")
                chunk_rows = c.execute(
                    "SELECT id, content FROM document_chunks WHERE document_id = ? LIMIT 2",
                    (doc["id"],)
                ).fetchall()
                c.close()
                if len(chunk_rows) == 1 and len(chunk_rows[0]["content"] or "") > 1000:
                    candidates.append({
                        "document_id": doc["id"],
                        "filename":    doc["filename"],
                        "content":     chunk_rows[0]["content"],
                    })
            except Exception:
                pass

        rows = candidates
        _rechunk_state["total"] = len(rows)

        for row in rows:
            doc_id   = row["document_id"]
            filename = row["filename"]
            content  = row["content"]
            parts    = split_text_into_chunks(content)
            _rechunk_state["done"] += 1

            if len(parts) <= 1:
                continue

            # Mit Timeout ausführen
            result_holder: list = []
            def _do(doc_id=doc_id, parts=parts, holder=result_holder):
                holder.append(_rechunk_one(doc_id, parts))

            t = threading.Thread(target=_do, daemon=True)
            t.start()
            t.join(timeout=DOC_TIMEOUT)

            if t.is_alive():
                # Timeout — überspringen
                reason = f"Timeout nach {DOC_TIMEOUT}s"
                log.warning("Rechunk timeout doc %s (%s)", doc_id, filename)
                _rechunk_state["skipped"] += 1
                _rechunk_state["failed_docs"].append(
                    {"id": doc_id, "filename": filename, "reason": reason}
                )
            elif result_holder and result_holder[0] is not None:
                # Fehler
                log.warning("Rechunk doc %s (%s): %s", doc_id, filename, result_holder[0])
                _rechunk_state["skipped"] += 1
                _rechunk_state["failed_docs"].append(
                    {"id": doc_id, "filename": filename, "reason": result_holder[0]}
                )
            else:
                _rechunk_state["fixed"] += 1

        _rechunk_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True, "message": "Re-chunk gestartet"})


@router.get("/ai/rechunk/status")
async def ai_rechunk_status():
    return JSONResponse(_rechunk_state)


@router.get("/ai/diagnostics")
async def ai_diagnostics():
    """Gibt den Embedding-Zustand als JSON zurück (läuft im Thread-Pool)."""
    import asyncio
    loop = asyncio.get_event_loop()

    def _query():
        conn = connection.get_connection()
        try:
            total_chunks = conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
            with_emb     = conn.execute(
                "SELECT COUNT(*) FROM document_chunks WHERE embedding IS NOT NULL"
            ).fetchone()[0]
            oversized = conn.execute("""
                SELECT COUNT(*) FROM (
                    SELECT dc.document_id
                    FROM document_chunks dc
                    JOIN documents d ON d.id = dc.document_id
                    WHERE d.extension NOT IN ('.pdf')
                    AND length(dc.content) > 1000
                    GROUP BY dc.document_id
                    HAVING COUNT(*) = 1
                )
            """).fetchone()[0]
            return {
                "total_chunks":           total_chunks,
                "with_embedding":         with_emb,
                "missing_embedding":      total_chunks - with_emb,
                "oversized_single_chunks": oversized,
                "embedding_coverage_pct": round(with_emb / total_chunks * 100, 1) if total_chunks else 0,
            }
        finally:
            conn.close()

    data = await loop.run_in_executor(None, _query)
    return JSONResponse(data)


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
