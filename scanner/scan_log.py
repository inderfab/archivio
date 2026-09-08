"""Protokoll jedes Projekt-Scans -- Startzeit, Dauer, Datei-Zähler, Fehler mit
Pfad, Spitzenwerte bei Speicher-/CPU-Nutzung. Grundlage für die Seite
/system-status. Wird ausschliesslich von web/dashboard.py::_run_scan() aufgerufen.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time

import psutil

log = logging.getLogger(__name__)


class ResourceSampler:
    """Sampelt periodisch Speicher (RSS, MB) und CPU (%) des aktuellen Prozesses
    UND seiner Kindprozesse (die Scanner-Worker laufen als eigene multiprocessing-
    Prozesse, siehe scanner/walker.py) und hält die Spitzenwerte fest. Läuft in
    einem eigenen Thread, damit die Messung den Scan selbst nicht verzögert."""

    def __init__(self, interval: float = 1.0):
        self.interval  = interval
        self.peak_mb   = 0.0
        self.peak_cpu  = 0.0
        self._stop     = threading.Event()
        self._thread   = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        me = psutil.Process(os.getpid())
        # cpu_percent() braucht einen ersten "Aufwärm"-Aufruf pro Prozess, sonst
        # liefert der erste echte Messwert immer 0.0 (Referenzzeitpunkt fehlt).
        try:
            me.cpu_percent()
        except Exception:
            pass
        while not self._stop.wait(self.interval):
            try:
                procs = [me] + me.children(recursive=True)
                mb  = sum(p.memory_info().rss for p in procs if p.is_running()) / (1024 * 1024)
                cpu = sum(p.cpu_percent() for p in procs if p.is_running())
                self.peak_mb  = max(self.peak_mb, mb)
                self.peak_cpu = max(self.peak_cpu, cpu)
            except Exception:
                pass  # Prozess kann zwischen children() und memory_info() verschwinden

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=self.interval + 1)


def log_scan(
    conn: sqlite3.Connection,
    project_id: int | None,
    project_name: str,
    progress: dict,
    started_at: str,
    finished_at: str,
    status: str,
    peak_memory_mb: float = 0.0,
    peak_cpu_pct: float = 0.0,
    batch_id: str | None = None,
) -> None:
    """Schreibt eine Protokollzeile. Fehlerdateien werden aus documents nachgeladen
    (extraction_status='error', seit started_at) statt separat während des Scans
    mitgeführt zu werden -- die Information steht nach dem Scan bereits vollständig
    in der DB, ein zweiter Tracking-Pfad über die Prozessgrenze der Worker hinweg
    wäre nur eine fehleranfällige Duplizierung derselben Wahrheit."""
    errors = []
    if project_id is not None:
        try:
            rows = conn.execute(
                """SELECT d.filename, dp.path FROM documents d
                   LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
                   WHERE d.project_id = ? AND d.extraction_status = 'error'
                     AND d.indexed_at >= ?""",
                (project_id, started_at),
            ).fetchall()
            errors = [{"filename": r["filename"], "path": r["path"]} for r in rows]
        except Exception as exc:
            log.warning("Fehlerdateien für Scan-Protokoll konnten nicht geladen werden: %s", exc)

    try:
        t0 = time.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ")
        t1 = time.strptime(finished_at, "%Y-%m-%dT%H:%M:%SZ")
        duration_s = time.mktime(t1) - time.mktime(t0)
    except Exception:
        duration_s = None

    with conn:
        conn.execute(
            """INSERT INTO scan_log
               (project_id, project_name, started_at, finished_at, duration_s, status,
                total, processed, new_count, skipped, error_count, errors_json,
                peak_memory_mb, peak_cpu_pct, batch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id, project_name, started_at, finished_at, duration_s, status,
                progress.get("total", 0), progress.get("processed", 0),
                progress.get("new", 0), progress.get("skipped", 0),
                len(errors) or progress.get("errors", 0), json.dumps(errors, ensure_ascii=False),
                round(peak_memory_mb, 1), round(peak_cpu_pct, 1), batch_id,
            ),
        )
