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
    for path in root.rglob("*"):
        if not path.is_file():
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
        file_hash = hasher.sha256(path)
    except OSError as exc:
        log.warning("Cannot read %s: %s", path, exc)
        return

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
