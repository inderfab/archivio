"""
SICHERHEIT — READ-ONLY-POLICY:
Der Scanner darf das NAS ausschliesslich lesen.
Verboten: os.remove, os.rename, shutil.delete, open(..., 'w'), open(..., 'wb'), Path.unlink, Path.rename.
Erlaubt:  open(..., 'rb'), open(..., 'r'), Path.stat(), os.walk(), Path.is_file().
"""
from __future__ import annotations

import logging
import os
import unicodedata
from pathlib import Path

from config import settings
from db import connection, queries
from scanner import hasher, extractors

log = logging.getLogger(__name__)


def _supported_extensions() -> set[str]:
    return {e.lower() for e in settings.get("scanner.supported_extensions", [])}


def _excluded_folders() -> set[str]:
    # NFC-Normalisierung: macOS SMB-Mounts liefern NFD-Ordnernamen
    return {unicodedata.normalize('NFC', f.lower()) for f in settings.get("scanner.excluded_folders", [])}


def scan_project(project_id: int, root: Path, progress: dict | None = None, cancel_flag: dict | None = None):
    """Walk root, hash every supported file, extract text, persist to DB."""
    supported = _supported_extensions()
    excluded = _excluded_folders()

    if progress is not None:
        progress["phase"] = "collecting"

    if not root.exists():
        log.error("Scan abgebrochen: Pfad existiert nicht: %s", root)
        if progress is not None:
            progress["phase"] = "error"
        return
    if not os.access(root, os.R_OK):
        log.error(
            "Scan abgebrochen: Kein Lesezugriff auf %s — "
            "macOS Full Disk Access in Systemeinstellungen → Datenschutz prüfen.", root
        )
        if progress is not None:
            progress["phase"] = "error"
        return

    batch: list[Path] = []

    # READ-ONLY: NAS darf nie verändert werden — os.walk() ist rein lesend.
    # dirnames[:] = [...] beschneidet nur die Walk-Liste, schreibt nichts auf das FS.
    for dirpath, dirnames, filenames in os.walk(root):
        # Ausgeschlossene Ordner in-place entfernen → os.walk steigt nicht hinein
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith('.')
            and not any(excl in unicodedata.normalize('NFC', d.lower()) for excl in excluded)
        ]
        for filename in filenames:
            if filename.startswith('.'):
                continue
            path = Path(dirpath) / filename
            if path.suffix.lower() not in supported:
                continue
            batch.append(path)

    if len(batch) == 0:
        log.warning(
            "Scan: 0 Dateien gefunden in %s — "
            "Mögliche Ursachen: Keine unterstützten Dateitypen (%s), "
            "macOS Full Disk Access fehlt, oder Pfad nicht zugänglich.",
            root,
            ", ".join(supported) if supported else "keine Extensions konfiguriert",
        )
    else:
        log.info(
            "Scan: %d Dateien gefunden (ohne Ordner: %s)",
            len(batch),
            ", ".join(settings.get("scanner.excluded_folders", [])),
        )

    if progress is not None:
        progress["phase"] = "processing"
        progress["total"] = len(batch)

    conn = connection.get_connection()
    for path in batch:
        if cancel_flag and cancel_flag.get("cancel"):
            log.info("Scan abgebrochen durch Benutzer nach %d/%d Dateien.",
                     progress.get("processed", 0) if progress else 0, len(batch))
            break
        if progress is not None:
            progress["current_file"] = path.name
        result = _process_file(conn, project_id, path)
        if progress is not None:
            progress["processed"] += 1
            if result == "new":
                progress["new"] += 1
            elif result == "skipped":
                progress["skipped"] += 1
            else:
                progress["errors"] += 1
    conn.close()


def _process_file(conn, project_id: int, path: Path) -> str:
    """Verarbeitet eine Datei. Gibt 'new', 'skipped' oder 'error' zurück."""
    try:
        # READ-ONLY: NAS darf nie verändert werden — sha256 öffnet nur mit 'rb'
        file_hash = hasher.sha256(path)
    except OSError as exc:
        log.warning("Kann nicht lesen %s: %s", path, exc)
        return "error"

    # Bereits vollständig indexiert → nur Pfad aktualisieren, Extraktion überspringen
    existing = conn.execute(
        "SELECT id, extraction_status FROM documents WHERE hash=?", (file_hash,)
    ).fetchone()
    if existing and existing["extraction_status"] == "ok":
        with conn:
            queries.upsert_path(conn, existing["id"], str(path), is_primary=True)
        return "skipped"

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
    return "new"


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
