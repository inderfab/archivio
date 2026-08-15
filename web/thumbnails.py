"""Thumbnail-Erzeugung für den Foto-Browser (#59).

Sicherheitsprinzip: Bilder werden NIE über einen vom Client übergebenen Pfad
ausgeliefert, sondern immer über document_id -- der Server löst den Pfad auf
und prüft ihn gegen dieselbe Whitelist (scanner.base_folders) wie list_folder
im MCP-Connector. Ein roher Pfad als Serve-Parameter wäre ein
Directory-Traversal-Loch.
"""
from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

from config import settings
from db import queries

log = logging.getLogger(__name__)

# Formate, die immer ein Thumbnail bekommen.
GALLERY_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".bmp", ".webp"}
# TIFF nur unterhalb der Schwelle -- grosse TIFFs sind oft 100+ MB und würden
# die Galerie spürbar verlangsamen.
TIFF_EXTENSIONS = {".tiff", ".tif"}
TIFF_MAX_BYTES = 20 * 1024 * 1024
ALL_GALLERY_EXTENSIONS = GALLERY_EXTENSIONS | TIFF_EXTENSIONS

SIZES = {"grid": 400, "full": 1600}

_heif_registered = False
_placeholder_cache: bytes | None = None


def _ensure_heif() -> None:
    global _heif_registered
    if _heif_registered:
        return
    _heif_registered = True
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception as e:
        log.warning("pillow-heif nicht verfügbar (HEIC/HEIF-Thumbnails schlagen fehl): %s", e)


def _data_dir() -> Path:
    d = os.environ.get("ARCHIVIO_DATA_DIR")
    return Path(d) if d else Path(__file__).parent.parent


def thumbnails_dir() -> Path:
    d = _data_dir() / "thumbnails"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_path(doc_hash: str, size_name: str) -> Path:
    sub = doc_hash[:2]
    d = thumbnails_dir() / sub
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{doc_hash}_{size_name}.jpg"


def clear_cache_for_hash(doc_hash: str) -> None:
    """Entfernt gecachte Thumbnails eines Dokuments (z.B. beim Löschen)."""
    for size_name in SIZES:
        try:
            _cache_path(doc_hash, size_name).unlink(missing_ok=True)
        except Exception:
            pass


def resolve_gallery_document(conn: sqlite3.Connection, document_id: int):
    """Löst document_id -> (Path, hash, extension) auf, geprüft gegen die
    konfigurierten NAS-Wurzelpfade. Gibt (None, None, None, Fehlermeldung)
    zurück, falls irgendetwas nicht stimmt -- nie eine Exception nach aussen."""
    row = conn.execute(
        """
        SELECT d.hash AS hash, d.extension AS extension, dp.path AS path
        FROM documents d
        JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        WHERE d.id = ?
        """,
        (document_id,),
    ).fetchone()
    if not row:
        return None, None, None, "Dokument nicht gefunden"

    base_folders = settings.get("scanner.base_folders", [])
    allowed = [f.get("path") for f in base_folders if f.get("path")]
    if not allowed:
        return None, None, None, "Keine NAS-Ordner konfiguriert"

    try:
        target = Path(row["path"]).resolve()
    except Exception as e:
        return None, None, None, f"Ungültiger Pfad: {e}"

    ok = any(
        target == Path(a).resolve() or Path(a).resolve() in target.parents
        for a in allowed
    )
    if not ok:
        return None, None, None, "Pfad ausserhalb der erlaubten Archivio-Ordner"
    if not target.exists():
        return None, None, None, "Datei nicht gefunden"

    return target, row["hash"], (row["extension"] or "").lower(), None


def get_thumbnail_bytes(conn: sqlite3.Connection, document_id: int, size_name: str) -> tuple[bytes, str]:
    """Gibt (jpeg_bytes, content_type) zurück -- bei jedem Fehler einen
    Platzhalter statt eine Exception/500, damit ein kaputtes Foto nie das
    ganze Grid killt."""
    size_name = size_name if size_name in SIZES else "grid"
    path, doc_hash, ext, err = resolve_gallery_document(conn, document_id)
    if err:
        log.debug("Thumbnail %s: %s", document_id, err)
        return _placeholder_bytes(), "image/jpeg"

    cache_file = _cache_path(doc_hash, size_name)
    if cache_file.exists():
        try:
            return cache_file.read_bytes(), "image/jpeg"
        except Exception:
            pass

    px = SIZES[size_name]
    data = _render_thumbnail(path, ext, px, document_id, conn)
    if data is None:
        return _placeholder_bytes(), "image/jpeg"
    try:
        cache_file.write_bytes(data)
    except Exception as e:
        log.warning("Thumbnail-Cache konnte nicht geschrieben werden: %s", e)
    return data, "image/jpeg"


def _render_thumbnail(path: Path, ext: str, px: int, document_id: int, conn: sqlite3.Connection) -> bytes | None:
    if ext in TIFF_EXTENSIONS:
        try:
            if path.stat().st_size > TIFF_MAX_BYTES:
                return None
        except Exception:
            return None
    elif ext not in GALLERY_EXTENSIONS:
        return None

    _ensure_heif()
    try:
        with Image.open(path) as im:
            _maybe_cache_exif_date(im, document_id, conn)
            im = ImageOps.exif_transpose(im)  # sonst stehen HEIC/JPEG-Thumbnails auf dem Kopf
            im = im.convert("RGB")
            im.thumbnail((px, px), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85)
            return buf.getvalue()
    except Exception as e:
        log.warning("Thumbnail fehlgeschlagen für %s: %s", path, e)
        return None


def _maybe_cache_exif_date(im: Image.Image, document_id: int, conn: sqlite3.Connection) -> None:
    """Liest DateTimeOriginal aus und cacht es in documents.metadata, damit die
    Galerie danach sortieren/gruppieren kann ohne bei jeder Anfrage EXIF neu zu
    lesen. Bestehend belassen falls schon gesetzt; Fehler werden verschluckt --
    das Sortierkriterium hat einen Fallback auf modified_at."""
    try:
        row = conn.execute("SELECT metadata FROM documents WHERE id = ?", (document_id,)).fetchone()
        if row:
            meta = json.loads(row["metadata"] or "{}")
            if meta.get("photo_taken_at"):
                return

        exif = im.getexif()
        dt_str = None
        try:
            exif_ifd = exif.get_ifd(0x8769)  # Exif-IFD
            dt_str = exif_ifd.get(36867) or exif_ifd.get(36868)  # DateTimeOriginal / Digitized
        except Exception:
            pass
        if not dt_str:
            dt_str = exif.get(306)  # DateTime (Top-Level)
        if not dt_str:
            return

        dt = datetime.strptime(str(dt_str).strip(), "%Y:%m:%d %H:%M:%S")
        queries.update_metadata(conn, document_id, {"photo_taken_at": dt.isoformat()})
        conn.commit()
    except Exception:
        pass


def _placeholder_bytes() -> bytes:
    global _placeholder_cache
    if _placeholder_cache is None:
        im = Image.new("RGB", (400, 400), (229, 231, 235))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=80)
        _placeholder_cache = buf.getvalue()
    return _placeholder_cache
