"""
SICHERHEIT — READ-ONLY-POLICY:
Der Scanner darf das NAS ausschliesslich lesen.
Verboten: os.remove, os.rename, shutil.delete, open(..., 'w'), open(..., 'wb'), Path.unlink, Path.rename.
Erlaubt:  open(..., 'rb'), open(..., 'r'), Path.stat(), Path.rglob(), Path.is_file().
"""

import logging
from pathlib import Path

from config import settings
from db import connection, queries
from scanner import hasher, extractors

log = logging.getLogger(__name__)


def _supported_extensions() -> set[str]:
    return {e.lower() for e in settings.get("scanner.supported_extensions", [])}


def scan_project(project_id: int, root: Path):
    """Walk root, hash every supported file, extract text, persist to DB."""
    supported = _supported_extensions()
    conn = connection.get_connection()

    batch: list[Path] = []
    # READ-ONLY: NAS darf nie verändert werden — rglob() ist rein lesend
    for path in root.rglob("*"):
        if not path.is_file():   # READ-ONLY: Metadaten-Abfrage, kein Schreiben
            continue
        if path.suffix.lower() not in supported:
            continue
        batch.append(path)

    log.info("Scanning %d files under %s", len(batch), root)

    for path in batch:
        _process_file(conn, project_id, path)

    conn.close()


def _process_file(conn, project_id: int, path: Path):
    try:
        # READ-ONLY: NAS darf nie verändert werden — sha256 öffnet nur mit 'rb'
        file_hash = hasher.sha256(path)
    except OSError as exc:
        log.warning("Cannot read %s: %s", path, exc)
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
        # READ-ONLY: NAS darf nie verändert werden — extract() öffnet nur lesend
        text, lang = extractors.extract(path)
        status = "ok"
    except extractors.UnsupportedFormat:
        text, lang, status = "", "", "unsupported"
    except extractors.ExtractionError as exc:
        log.warning("Extraction failed for %s: %s", path, exc)
        text, lang, status = "", "", "error"
    except Exception as exc:
        log.error("Unexpected error for %s: %s", path, exc)
        text, lang, status = "", "", "error"

    with conn:
        queries.set_extraction_status(conn, doc_id, status)
        if text:
            queries.upsert_content(conn, doc_id, text, lang)


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
