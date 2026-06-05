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
import threading
import unicodedata
from pathlib import Path

from config import settings
from db import connection, queries
from scanner import hasher, extractors

log = logging.getLogger(__name__)

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
    """Verarbeitet eine einzelne Datei im Pool-Prozess.
    Der Prozess wird nach maxtasksperchild Dateien ersetzt → vollständige
    Speicherfreigabe durch OS, kein Python-Allocator-Wachstum.
    """
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


def scan_project(project_id: int, root: Path,
                 progress: dict | None = None,
                 cancel_flag: dict | None = None):
    """Walk root, extract text, embed, persist to DB.

    Jede Datei wird in einem eigenen Pool-Workerprozess verarbeitet.
    Nach maxtasksperchild Dateien wird der Worker-Prozess ersetzt —
    das OS gibt dabei ALLEN Speicher frei (PyMuPDF-Cache, Python-Allocator,
    eingeladene Bibliotheken). So akkumuliert kein RAM über Hunderte von Dateien.
    """
    supported = _supported_extensions()
    excluded  = _excluded_folders()
    tasks_per_worker = max(5, int(settings.get("scanner.tasks_per_worker", 20)))

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

    log.info("Scan: %d Dateien in %s (Worker-Reset alle %d Dateien)",
             len(batch), root, tasks_per_worker)

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

            try:
                result = pool.apply(_scan_file_worker, ((project_id, str(path)),))
            except Exception as exc:
                log.warning("Pool-Fehler bei %s: %s", path.name, exc)
                result = "error"

            if progress is not None:
                progress["processed"] += 1
                if result == "new":       progress["new"] += 1
                elif result == "skipped": progress["skipped"] += 1
                else:                     progress["errors"] += 1
    finally:
        pool.close()
        pool.join()


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

    # Grössencheck: Formate die alles in RAM laden
    size_mb = stat.st_size / (1024 * 1024)
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
