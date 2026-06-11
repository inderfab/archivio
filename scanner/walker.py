"""
SICHERHEIT — READ-ONLY-POLICY:
Der Scanner darf das NAS ausschliesslich lesen.
Verboten: os.remove, os.rename, shutil.delete, open(..., 'w'), open(..., 'wb'), Path.unlink, Path.rename.
Erlaubt:  open(..., 'rb'), open(..., 'r'), Path.stat(), os.walk(), Path.is_file().
"""
from __future__ import annotations

import gc
import logging
import multiprocessing
import os
import signal
import threading
import time
import unicodedata
from pathlib import Path

from config import settings
from db import connection, queries
from scanner import hasher, extractors

log = logging.getLogger(__name__)

_TASK_TIMEOUT     = 300   # Sekunden pro Datei — dann SIGKILL
_MAX_WORKER_RSS   = 3.0   # GB — Worker wird per SIGKILL beendet wenn überschritten
_POLL_INTERVAL    = 5     # Sekunden zwischen RSS-Checks
_MAX_PDF_EXTRACT_MB = 500 # PDFs grösser als 500 MB werden nur registriert

_LIST_ONLY_EXTENSIONS = {
    ".c4d", ".tiff", ".tif", ".png", ".jpg", ".jpeg",
    ".xml", ".3ds", ".obj", ".stp", ".step", ".stl", ".tx",
}

# Formate die komplett in RAM geladen werden → Grössencheck
_SIZE_LIMITED_EXTENSIONS = {".docx", ".doc", ".xlsx", ".rtf"}
_MAX_EXTRACT_MB = 30  # Dateien > 30 MB werden nur registriert, nicht extrahiert


def _supported_extensions() -> set[str]:
    return {e.lower() for e in settings.get("scanner.supported_extensions", [])}


def _excluded_folders() -> set[str]:
    return {unicodedata.normalize('NFC', f.lower())
            for f in settings.get("scanner.excluded_folders", [])}


# ── Worker-Funktion (läuft im Pool-Prozess) ────────────────────────────────────
# Muss auf Modul-Ebene stehen damit multiprocessing sie pickeln kann.

def _scan_file_worker(args: tuple) -> str:
    """Verarbeitet eine einzelne Datei im Pool-Prozess."""
    extractors._IN_WORKER_PROCESS = True
    project_id, path_str = args
    conn = connection.get_connection()
    try:
        return _process_file(conn, project_id, Path(path_str))
    except Exception as exc:
        log.warning("Worker-Fehler %s: %s", Path(path_str).name, exc)
        return "error"
    finally:
        conn.close()
        gc.collect()
        try:
            import fitz
            fitz.TOOLS.store_shrink(100)
        except Exception:
            pass


def _kill_workers(pool) -> None:
    """Sendet SIGKILL an alle Worker-Prozesse des Pools und wartet auf deren Ende."""
    for w in getattr(pool, "_pool", []):
        try:
            if w.is_alive():
                os.kill(w.pid, signal.SIGKILL)
                log.info("SIGKILL → Worker PID %d", w.pid)
        except Exception:
            pass
    try:
        pool.terminate()
        pool.join()
    except Exception:
        pass


def _worker_rss_gb(pool) -> float:
    """RSS des laufenden Workers in GB, oder 0 wenn nicht ermittelbar."""
    try:
        import psutil
        for w in getattr(pool, "_pool", []):
            if w.is_alive():
                return psutil.Process(w.pid).memory_info().rss / (1024 ** 3)
    except Exception:
        pass
    return 0.0


def scan_project(project_id: int, root: Path,
                 progress: dict | None = None,
                 cancel_flag: dict | None = None):
    """Walk root, extract text, embed, persist to DB.

    Jede Datei läuft in einem Pool-Worker (processes=1).
    Der Hauptprozess überwacht alle _POLL_INTERVAL Sekunden den RSS des Workers.
    Bei Überschreitung von _MAX_WORKER_RSS GB oder _TASK_TIMEOUT Sekunden:
    SIGKILL → sofortige Speicherfreigabe → neuer Pool.
    """
    supported = _supported_extensions()
    excluded  = _excluded_folders()
    tasks_per_worker = max(3, int(settings.get("scanner.tasks_per_worker", 5)))

    if progress is not None:
        progress["phase"] = "collecting"

    if not root.exists():
        msg = f"Pfad existiert nicht: {root}"
        log.error("Scan abgebrochen: %s", msg)
        if progress is not None:
            progress["phase"] = "error"
            progress["error"] = msg
        return
    if not os.access(root, os.R_OK):
        msg = f"Kein Lesezugriff auf {root}"
        log.error("Scan abgebrochen: %s", msg)
        if progress is not None:
            progress["phase"] = "error"
            progress["error"] = msg
        return

    batch: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith('.')
            and not any(excl in unicodedata.normalize('NFC', d.lower())
                        for excl in excluded)
        ]
        for filename in filenames:
            if filename.startswith('.'):
                continue
            path = Path(dirpath) / filename
            if path.suffix.lower() not in supported:
                continue
            batch.append(path)

    if not batch:
        log.warning("Scan: 0 Dateien gefunden in %s", root)
        return

    log.info("Scan: %d Dateien in %s", len(batch), root)

    if progress is not None:
        progress["phase"]     = "processing"
        progress["total"]     = len(batch)
        progress["processed"] = 0
        progress["new"]       = 0
        progress["skipped"]   = 0
        progress["errors"]    = 0

    ctx = multiprocessing.get_context("spawn")
    pool = ctx.Pool(processes=1, maxtasksperchild=tasks_per_worker)

    try:
        for path in batch:
            if cancel_flag and cancel_flag.get("cancel"):
                log.info("Scan abgebrochen.")
                break
            if progress is not None:
                progress["current_file"] = path.name

            ar     = pool.apply_async(_scan_file_worker, ((project_id, str(path)),))
            start  = time.monotonic()
            result = None

            # Polling-Loop: alle _POLL_INTERVAL Sekunden RSS prüfen
            while result is None:
                try:
                    result = ar.get(timeout=_POLL_INTERVAL)
                except multiprocessing.TimeoutError:
                    elapsed = time.monotonic() - start
                    rss     = _worker_rss_gb(pool)

                    if rss > _MAX_WORKER_RSS:
                        log.warning("Worker RAM %.1f GB > %.1f GB — SIGKILL: %s",
                                    rss, _MAX_WORKER_RSS, path.name)
                        _kill_workers(pool)
                        pool = ctx.Pool(processes=1, maxtasksperchild=tasks_per_worker)
                        result = "error"
                    elif elapsed > _TASK_TIMEOUT:
                        log.warning("Datei-Timeout (%ds): %s — SIGKILL",
                                    _TASK_TIMEOUT, path.name)
                        _kill_workers(pool)
                        pool = ctx.Pool(processes=1, maxtasksperchild=tasks_per_worker)
                        result = "error"
                except Exception as exc:
                    log.warning("Pool-Fehler bei %s: %s", path.name, exc)
                    result = "error"

            if progress is not None:
                progress["processed"] += 1
                if result == "new":       progress["new"] += 1
                elif result == "skipped": progress["skipped"] += 1
                else:                     progress["errors"] += 1

            # WAL-Checkpoint alle 100 Dateien
            if progress is not None and progress["processed"] % 100 == 0:
                try:
                    _wal_conn = connection.get_connection()
                    _wal_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    _wal_conn.close()
                except Exception:
                    pass

    finally:
        _kill_workers(pool)


def _process_file(conn, project_id: int, path: Path) -> str:
    try:
        stat = path.stat()
    except OSError as exc:
        log.warning("Kann nicht lesen %s: %s", path, exc)
        return "error"

    ext       = path.suffix.lower()
    mtime_iso = _iso(stat.st_mtime)

    # Schnellpfad: Metadaten unverändert → überspringen (inkl. error/unsupported)
    fast = conn.execute("""
        SELECT d.id FROM document_paths dp
        JOIN   documents d ON d.id = dp.document_id
        WHERE  dp.path        = ?
          AND  d.filesize     = ?
          AND  d.modified_at  = ?
          AND  d.extraction_status IN ('ok', 'listed', 'error', 'unsupported')
    """, (str(path), stat.st_size, mtime_iso)).fetchone()
    if fast:
        return "skipped"

    try:
        file_hash = hasher.sha256(path)
    except OSError as exc:
        log.warning("Kann nicht lesen %s: %s", path, exc)
        return "error"

    existing = conn.execute(
        "SELECT id, extraction_status FROM documents WHERE hash=?", (file_hash,)
    ).fetchone()
    if existing and existing["extraction_status"] in ("ok", "listed"):
        with conn:
            queries.upsert_path(conn, existing["id"], str(path), is_primary=True)
        return "skipped"

    data = {
        "project_id":  project_id,
        "hash":        file_hash,
        "filename":    path.name,
        "extension":   ext,
        "filesize":    stat.st_size,
        "modified_at": mtime_iso,
        "source_type": "filesystem",
    }
    with conn:
        doc_id = queries.upsert_document(conn, data)
        queries.upsert_path(conn, doc_id, str(path), is_primary=True)

    if ext in _LIST_ONLY_EXTENSIONS:
        with conn:
            queries.set_extraction_status(conn, doc_id, "listed")
        return "new"

    size_mb = stat.st_size / (1024 * 1024)

    # Grosse PDFs (Pläne, Scan-Archive) nur registrieren
    if ext == ".pdf" and size_mb > _MAX_PDF_EXTRACT_MB:
        log.warning("PDF zu gross (%.0f MB > %d MB): %s",
                    size_mb, _MAX_PDF_EXTRACT_MB, path.name)
        with conn:
            queries.set_extraction_status(conn, doc_id, "listed")
        return "new"

    # Grössencheck: Formate die alles in RAM laden
    if ext in _SIZE_LIMITED_EXTENSIONS and size_mb > _MAX_EXTRACT_MB:
        log.warning("Datei zu gross für Extraktion (%.1f MB > %d MB): %s",
                    size_mb, _MAX_EXTRACT_MB, path.name)
        with conn:
            queries.set_extraction_status(conn, doc_id, "listed")
        return "new"

    _extract_and_store(conn, doc_id, path)
    return "new"


_EXTRACT_TIMEOUT = 180


def _extract_and_store(conn, doc_id: int, path: Path):
    if extractors._IN_WORKER_PROCESS:
        # Im Worker-Prozess: direkt aufrufen — der Pool-Timeout (_TASK_TIMEOUT) ist
        # die Sicherheitsgrenze, kein Thread nötig.
        try:
            chunks = extractors.extract_chunks(path)
            text   = "\n".join(c["content"] for c in chunks)
            status = "ok"
        except extractors.UnsupportedFormat:
            chunks, text, status = [], "", "unsupported"
        except Exception as exc:
            log.warning("Extraktion fehlgeschlagen %s: %s", path.name, exc)
            chunks, text, status = [], "", "error"
    else:
        result_box: list = []
        error_box:  list = []

        def _do_extract():
            try:
                result_box.append(extractors.extract_chunks(path))
            except Exception as exc:
                error_box.append(exc)

        t = threading.Thread(target=_do_extract, daemon=True)
        t.start()
        t.join(timeout=_EXTRACT_TIMEOUT)

        if t.is_alive():
            log.warning("Extraktion Timeout (%ds): %s", _EXTRACT_TIMEOUT, path.name)
            chunks, text, status = [], "", "error"
        elif error_box:
            exc = error_box[0]
            if isinstance(exc, extractors.UnsupportedFormat):
                chunks, text, status = [], "", "unsupported"
            else:
                log.warning("Extraktion fehlgeschlagen %s: %s", path.name, exc)
                chunks, text, status = [], "", "error"
        else:
            chunks = result_box[0] if result_box else []
            text   = "\n".join(c["content"] for c in chunks)
            status = "ok"

    with conn:
        queries.set_extraction_status(conn, doc_id, status)
        if text:
            queries.upsert_content(conn, doc_id, text, "")
        if chunks:
            queries.save_chunks(conn, doc_id, chunks)

    if chunks:
        try:
            from scanner.embedder import embed_document_chunks, is_ollama_running
            if is_ollama_running():
                embed_document_chunks(conn, doc_id)
        except Exception as e:
            log.debug("Embedding übersprungen für %s: %s", path.name, e)


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
