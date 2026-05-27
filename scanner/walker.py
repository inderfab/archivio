"""
SICHERHEIT — READ-ONLY-POLICY:
Der Scanner darf das NAS ausschliesslich lesen.
Verboten: os.remove, os.rename, shutil.delete, open(..., 'w'), open(..., 'wb'), Path.unlink, Path.rename.
Erlaubt:  open(..., 'rb'), open(..., 'r'), Path.stat(), os.walk(), Path.is_file().
"""

import logging
import os
from pathlib import Path

from config import settings
from db import connection, queries
from scanner import hasher, extractors

log = logging.getLogger(__name__)


def _supported_extensions() -> set[str]:
    return {e.lower() for e in settings.get("scanner.supported_extensions", [])}


def _excluded_folders() -> set[str]:
    # Case-insensitiv vergleichen für Robustheit
    return {f.lower() for f in settings.get("scanner.excluded_folders", [])}


def scan_project(project_id: int, root: Path):
    """Walk root, hash every supported file, extract text, persist to DB."""
    supported = _supported_extensions()
    excluded = _excluded_folders()

    batch: list[Path] = []

    # READ-ONLY: NAS darf nie verändert werden — os.walk() ist rein lesend.
    # dirnames[:] = [...] beschneidet nur die Walk-Liste, schreibt nichts auf das FS.
    for dirpath, dirnames, filenames in os.walk(root):
        # Ausgeschlossene Ordner in-place entfernen → os.walk steigt nicht hinein
        dirnames[:] = [
            d for d in dirnames
            if d.lower() not in excluded
        ]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() not in supported:
                continue
            batch.append(path)

    log.info(
        "Scan: %d Dateien gefunden (ohne Ordner: %s)",
        len(batch),
        ", ".join(settings.get("scanner.excluded_folders", [])),
    )

    conn = connection.get_connection()
    for path in batch:
        _process_file(conn, project_id, path)
    conn.close()


def _process_file(conn, project_id: int, path: Path):
    try:
        # READ-ONLY: NAS darf nie verändert werden — sha256 öffnet nur mit 'rb'
        file_hash = hasher.sha256(path)
    except OSError as exc:
        log.warning("Kann nicht lesen %s: %s", path, exc)
        return

    # READ-ONLY: NAS darf nie verändert werden — stat() liest nur Metadaten
    stat = path.stat()
    data = {
        "project_id": project_id,
        "hash": file_hash,
        "filename": path.name,
        "extension": path.suffix.lower(),
        "filesize": stat.st_size,
        "modified_at": _iso(stat.st_mtime),
        "source_type": "filesystem",
    }

    with conn:
        doc_id = queries.upsert_document(conn, data)
        queries.upsert_path(conn, doc_id, str(path), is_primary=True)

    _extract_and_store(conn, doc_id, path)


def _extract_and_store(conn, doc_id: int, path: Path):
    try:
        # READ-ONLY: NAS darf nie verändert werden — extract_chunks() öffnet nur lesend
        chunks = extractors.extract_chunks(path)
        text   = "\n".join(c["content"] for c in chunks)
        status = "ok"
    except extractors.UnsupportedFormat:
        chunks, text, status = [], "", "unsupported"
    except extractors.ExtractionError as exc:
        log.warning("Extraktion fehlgeschlagen %s: %s", path, exc)
        chunks, text, status = [], "", "error"
    except Exception as exc:
        log.error("Unerwarteter Fehler %s: %s", path, exc)
        chunks, text, status = [], "", "error"

    with conn:
        queries.set_extraction_status(conn, doc_id, status)
        if text:
            queries.upsert_content(conn, doc_id, text, "")
        if chunks:
            queries.save_chunks(conn, doc_id, chunks)


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
