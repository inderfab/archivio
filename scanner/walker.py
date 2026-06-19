"""
SICHERHEIT — READ-ONLY-POLICY:
Der Scanner darf das NAS ausschliesslich lesen.
Verboten: os.remove, os.rename, shutil.delete, open(..., 'w'), open(..., 'wb'), Path.unlink, Path.rename.
Erlaubt:  open(..., 'rb'), open(..., 'r'), Path.stat(), os.walk(), Path.is_file().
"""
from __future__ import annotations

import atexit
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

# ── Globales Worker-PID-Register ──────────────────────────────────────────────
# Alle laufenden Scanner-Worker-PIDs — überlebt Pool-Neuerstellungen.
# Wird von atexit + SIGTERM-Handler genutzt um Worker beim App-Quit zu killen.
_worker_pids: set[int] = set()
_worker_pids_lock      = threading.Lock()


def kill_all_workers() -> None:
    """SIGKILL alle bekannten Scanner-Worker — für Cancel, App-Quit, SIGTERM."""
    with _worker_pids_lock:
        pids = list(_worker_pids)
        _worker_pids.clear()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            log.info("kill_all_workers: SIGKILL → PID %d", pid)
        except ProcessLookupError:
            pass
        except Exception as exc:
            log.warning("kill_all_workers PID %d: %s", pid, exc)


atexit.register(kill_all_workers)


def _worker_watchdog_init(parent_pid: int) -> None:
    """Läuft im Worker-Prozess: Watchdog-Thread der Parent überwacht.
    Wenn Parent stirbt (App-Quit ohne sauberes Cleanup) → SIGKILL sich selbst.
    """
    extractors._IN_WORKER_PROCESS = True

    # fitz-internen Cache begrenzen — verhindert unkontrolliertes Wachstum bei grossen PDFs
    try:
        import fitz
        fitz.TOOLS.store_maxsize = 200 * 1024 * 1024  # 200 MB Cache-Limit
    except Exception:
        pass

    def _watch():
        while True:
            time.sleep(3)
            try:
                os.kill(parent_pid, 0)  # prüft ob Parent-Prozess noch existiert
            except ProcessLookupError:
                os.kill(os.getpid(), signal.SIGKILL)
            except Exception:
                pass

    t = threading.Thread(target=_watch, daemon=True)
    t.start()


def _track_pool(pool) -> None:
    """Trägt Worker-PIDs des Pools in die globale Menge ein."""
    for w in getattr(pool, "_pool", []):
        if w.pid:
            with _worker_pids_lock:
                _worker_pids.add(w.pid)


_memory_watchdog_started = False
_memory_watchdog_lock    = threading.Lock()


def _start_memory_watchdog() -> None:
    """Startet (einmalig) einen Hintergrund-Thread der jede Sekunde alle
    bekannten Worker-PIDs überwacht — unabhängig vom Scan-Polling-Loop.

    Fängt verwaiste Prozesse (maxtasksperchild-Rotation, D-State) ab,
    die der Polling-Loop nicht sieht weil er nur pool._pool prüft.
    """
    global _memory_watchdog_started
    with _memory_watchdog_lock:
        if _memory_watchdog_started:
            return
        _memory_watchdog_started = True

    def _watch():
        while True:
            time.sleep(1)
            try:
                rss      = _total_workers_rss_gb()
                pressure = _system_under_pressure()
                if rss > _MAX_WORKER_RSS:
                    with _worker_pids_lock:
                        pids = list(_worker_pids)
                    if pids:
                        log.warning(
                            "Memory-Watchdog: RSS=%.1f GB, Druck=%s → SIGKILL %s",
                            rss, pressure, pids,
                        )
                    for pid in pids:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        except Exception as exc:
                            log.warning("Watchdog SIGKILL PID %d: %s", pid, exc)
            except Exception:
                pass

    threading.Thread(target=_watch, daemon=True, name="memory-watchdog").start()

_POLL_INTERVAL      = 2    # Sekunden zwischen Speicher-Checks (vorher 5)
_MAX_PDF_EXTRACT_MB = 500  # PDFs grösser als 500 MB werden nur registriert

# RAM-Limits dynamisch je nach verfügbarem Systemspeicher
def _auto_limits() -> tuple[float, int, float]:
    """Gibt (_MAX_WORKER_RSS in GB, _TASK_TIMEOUT in Sekunden, _MIN_FREE_GB) zurück."""
    try:
        import psutil
        total_gb = psutil.virtual_memory().total / (1024 ** 3)
    except Exception:
        total_gb = 16.0
    rss_limit  = max(3.0, total_gb * 0.20)       # 20% des RAM (64 GB → ~12.8 GB)
    timeout    = max(120, int(total_gb * 10))   # 10s pro GB RAM (64 GB → 640s)
    min_free   = max(2.0, min(4.0, total_gb * 0.08))  # max 4 GB — 20% war zu aggressiv für grosse Maschinen
    return rss_limit, timeout, min_free

_MAX_WORKER_RSS, _TASK_TIMEOUT, _MIN_FREE_GB = _auto_limits()


def _system_under_pressure() -> bool:
    """True wenn das System systemweit unter Speicherdruck steht.
    Misst verfügbaren RAM — reagiert auf Swap-Nutzung die RSS nicht zeigt.
    """
    try:
        import psutil
        vm = psutil.virtual_memory()
        return (vm.available / (1024 ** 3)) < _MIN_FREE_GB
    except Exception:
        return False

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
    """SIGKILL alle Worker — sofort, ohne auf pool.terminate() zu warten."""
    workers = list(getattr(pool, "_pool", []))

    # Alle bekannten PIDs (Pool + globales Register) sofort killen
    pids: set[int] = set()
    for w in workers:
        if getattr(w, "pid", None):
            pids.add(w.pid)
    with _worker_pids_lock:
        pids.update(_worker_pids)

    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            log.info("SIGKILL → Worker PID %d", pid)
        except ProcessLookupError:
            pass
        except Exception as exc:
            log.warning("SIGKILL PID %d: %s", pid, exc)

    with _worker_pids_lock:
        for pid in pids:
            _worker_pids.discard(pid)

    # pool.terminate() im Hintergrund — räumt interne Queues auf, darf hängen
    def _do_terminate():
        try:
            pool.terminate()
        except Exception:
            pass
    threading.Thread(target=_do_terminate, daemon=True).start()


def _total_workers_rss_gb() -> float:
    """Summe des RSS ALLER bekannten Worker-PIDs — inkl. verwaister Prozesse.

    Tote PIDs werden automatisch aus dem Register entfernt.
    Ohne diese Funktion blieb ein 56GB-Zombie aus maxtasksperchild-Rotation
    unbemerkt, weil pool._pool nur den aktuellen Worker enthält.
    """
    try:
        import psutil
        total = 0.0
        dead: set[int] = set()
        with _worker_pids_lock:
            pids = list(_worker_pids)
        for pid in pids:
            try:
                total += psutil.Process(pid).memory_info().rss / (1024 ** 3)
            except (psutil.NoSuchProcess, ProcessLookupError):
                dead.add(pid)
            except Exception:
                pass
        if dead:
            with _worker_pids_lock:
                _worker_pids.difference_update(dead)
        return total
    except Exception:
        return 0.0


def scan_project(project_id: int, root: Path,
                 progress: dict | None = None,
                 cancel_flag: dict | None = None):
    """Walk root, extract text, embed, persist to DB.

    Jede Datei läuft in einem oder mehreren Pool-Workern (konfigurierbar via scanner.num_workers).
    Der Hauptprozess überwacht alle _POLL_INTERVAL Sekunden den RSS des Workers.
    Bei Überschreitung von _MAX_WORKER_RSS GB oder _TASK_TIMEOUT Sekunden:
    SIGKILL → sofortige Speicherfreigabe → neuer Pool.
    """
    supported = _supported_extensions()
    excluded  = _excluded_folders()
    tasks_per_worker = max(3, int(settings.get("scanner.tasks_per_worker", 5)))
    num_workers      = max(1, min(4, int(settings.get("scanner.num_workers", 1))))

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

    if progress is not None:
        progress["phase"]     = "processing"
        progress["total"]     = 0
        progress["processed"] = 0
        progress["new"]       = 0
        progress["skipped"]   = 0
        progress["errors"]    = 0

    _start_memory_watchdog()  # einmalig starten (noop wenn bereits läuft)

    # FTS5-Automerge während des Scans deaktivieren — bei grossen Archiven (>10k Dokumente)
    # löst der automatische Segment-Merge mitten im Worker-Commit aus und blockiert
    # den Prozess für Minuten (erscheint als "hängt bei 99%").
    # Nach dem Scan wird einmalig optimize() aufgerufen.
    try:
        _fts_conn = connection.get_connection()
        _fts_conn.execute("INSERT INTO documents_fts(documents_fts) VALUES('automerge=0')")
        _fts_conn.commit()
        _fts_conn.close()
    except Exception:
        pass

    ctx = multiprocessing.get_context("spawn")
    pool = ctx.Pool(
        processes=num_workers,
        maxtasksperchild=tasks_per_worker,
        initializer=_worker_watchdog_init,
        initargs=(os.getpid(),),
    )
    _track_pool(pool)

    found_any = False
    try:
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
                found_any = True
                if progress is not None:
                    progress["total"] += 1

                if cancel_flag and cancel_flag.get("cancel"):
                    log.info("Scan abgebrochen.")
                    return

                # Vor jeder Datei: System-Speicher prüfen — wenn knapp, kurz warten
                _pressure_waits = 0
                while _system_under_pressure() and _pressure_waits < 6:
                    log.info("Speicherdruck — warte 10s vor nächster Datei (%s)", path.name)
                    time.sleep(10)
                    gc.collect()
                    _pressure_waits += 1
                if _pressure_waits >= 6:
                    log.warning("Speicherdruck hält an — %s übersprungen", path.name)
                    if progress is not None:
                        progress["processed"] += 1
                        progress["errors"]    += 1
                    continue

                if progress is not None:
                    progress["current_file"]   = path.name
                    progress["current_folder"] = path.parent.name

                ar     = pool.apply_async(_scan_file_worker, ((project_id, str(path)),))
                start  = time.monotonic()
                result = None
                # Nicht-PDF-Formate (DOCX, EML, RTF …) haben zwar SIGALRM (30s), aber
                # auf macOS blockiert NAS-I/O den Syscall und SIGALRM kommt nicht durch.
                # Daher max 120s als harte Grenze im Polling-Loop.
                file_timeout = _TASK_TIMEOUT if path.suffix.lower() == ".pdf" else min(_TASK_TIMEOUT, 120)

                # Polling-Loop: alle _POLL_INTERVAL Sekunden RSS + System-RAM prüfen
                while result is None:
                    try:
                        result = ar.get(timeout=_POLL_INTERVAL)
                    except multiprocessing.TimeoutError:
                        elapsed  = time.monotonic() - start
                        rss      = _total_workers_rss_gb()   # ALLE PIDs, nicht nur pool._pool
                        pressure = _system_under_pressure()

                        if rss > _MAX_WORKER_RSS:
                            log.warning("Worker RSS %.1f GB > %.1f GB — SIGKILL: %s",
                                        rss, _MAX_WORKER_RSS, path.name)
                            _kill_workers(pool)
                            pool = ctx.Pool(processes=num_workers, maxtasksperchild=tasks_per_worker,
                                            initializer=_worker_watchdog_init, initargs=(os.getpid(),))
                            _track_pool(pool)
                            result = "error"
                        elif pressure:
                            log.warning("System-Speicherdruck — SIGKILL Worker: %s", path.name)
                            _kill_workers(pool)
                            pool = ctx.Pool(processes=num_workers, maxtasksperchild=tasks_per_worker,
                                            initializer=_worker_watchdog_init, initargs=(os.getpid(),))
                            _track_pool(pool)
                            result = "error"
                        elif elapsed > file_timeout:
                            log.warning("Datei-Timeout (%ds): %s — SIGKILL",
                                        file_timeout, path.name)
                            _kill_workers(pool)
                            pool = ctx.Pool(processes=num_workers, maxtasksperchild=tasks_per_worker,
                                            initializer=_worker_watchdog_init, initargs=(os.getpid(),))
                            _track_pool(pool)
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
                        _wal_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                        _wal_conn.close()
                    except Exception:
                        pass

        if not found_any:
            log.warning("Scan: 0 Dateien gefunden in %s", root)

    finally:
        _kill_workers(pool)

    # FTS5-Automerge reaktivieren und einmaliges Optimize anstoßen (Hintergrund-Thread)
    def _fts_optimize():
        try:
            c = connection.get_connection()
            c.execute("INSERT INTO documents_fts(documents_fts) VALUES('automerge=8')")
            c.commit()
            c.execute("INSERT INTO documents_fts(documents_fts) VALUES('optimize')")
            c.commit()
            c.close()
            log.info("FTS5 optimize abgeschlossen")
        except Exception as exc:
            log.warning("FTS5 optimize fehlgeschlagen: %s", exc)
    threading.Thread(target=_fts_optimize, daemon=True, name="fts-optimize").start()


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


_EXTRACT_TIMEOUT = 30


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

    # PDF-Metadaten (Creator/Producer) für Plan-Erkennung
    if path.suffix.lower() == ".pdf":
        try:
            meta = extractors.extract_pdf_metadata(path)
            if meta:
                with conn:
                    queries.update_metadata(conn, doc_id, meta)
        except Exception:
            pass
    # Kein Embedding im Worker — läuft nach dem Scan als separater Schritt
    # (verhindert dass ein langsamer Ollama-Call den ganzen Scan blockiert)


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
