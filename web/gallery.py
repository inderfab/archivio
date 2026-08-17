"""Foto-Browser: Galerie-Ansicht mit Sternebewertung (#59).

Bildauslieferung ausschliesslich über document_id (siehe web/thumbnails.py) --
nie ein Dateipfad als Client-Parameter.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from db import connection, queries
from web.shared import templates
from web.thumbnails import ALL_GALLERY_EXTENSIONS, get_thumbnail_bytes

router = APIRouter()
log = logging.getLogger(__name__)

PAGE_SIZE = 30
_GALLERY_EXTS = sorted(ALL_GALLERY_EXTENSIONS)

# Format-Filter in der UI: zusammengefasste Gruppen (jpg+jpeg, heic+heif, tiff+tif)
# statt jede Extension einzeln -- aus Nutzersicht ist "HEIC" eine Auswahl, nicht zwei.
FORMAT_GROUPS = {
    "jpg":  [".jpg", ".jpeg"],
    "png":  [".png"],
    "heic": [".heic", ".heif"],
    "gif":  [".gif"],
    "bmp":  [".bmp"],
    "webp": [".webp"],
    "tiff": [".tiff", ".tif"],
}


def _project_filter_sql(project_id: str) -> tuple[str, list]:
    if not project_id:
        return "", []
    if project_id.startswith("mailbox:"):
        return " AND 1=0", []  # Postfächer haben keine Foto-Galerie
    try:
        pid = int(project_id)
    except ValueError:
        return "", []
    return (
        " AND (d.project_id IN ("
        "  SELECT p2.id FROM projects p2"
        "  JOIN projects p1 ON (p2.path = p1.path OR p2.path LIKE (p1.path || '/%'))"
        "  WHERE p1.id = ?"
        ") OR d.id IN ("
        "  SELECT dp2.document_id FROM document_paths dp2"
        "  JOIN projects p3 ON dp2.path LIKE (p3.path || '/%')"
        "  WHERE p3.id = ?"
        "))"
    ), [pid, pid]


def _rating_filter_sql(sterne: str) -> tuple[str, list]:
    if not sterne or sterne == "alle":
        return "", []
    if sterne == "unbewertet":
        return " AND r.rating IS NULL", []
    try:
        n = int(sterne)
    except ValueError:
        return "", []
    if n < 1 or n > 5:
        return "", []
    return " AND r.rating >= ?", [n]


def _parent_folder_name(path: str) -> str:
    return Path(path).parent.name or "/"


def _format_filter_sql(formate: str) -> tuple[str, list]:
    """formate = kommagetrennte Format-Gruppen-Keys (z.B. 'jpg,heic'). Leer = kein Filter."""
    if not formate:
        return "", []
    exts: list[str] = []
    for key in formate.split(","):
        exts.extend(FORMAT_GROUPS.get(key.strip(), []))
    if not exts:
        return "", []
    placeholders = ",".join("?" * len(exts))
    return f" AND d.extension IN ({placeholders})", exts


def _date_filter_sql(date_from: str, date_to: str) -> tuple[str, list]:
    sql, params = "", []
    date_expr = "COALESCE(json_extract(d.metadata, '$.photo_taken_at'), d.modified_at)"
    if date_from:
        sql += f" AND {date_expr} >= ?"
        params.append(date_from)
    if date_to:
        sql += f" AND {date_expr} <= ?"
        params.append(date_to + "T23:59:59")
    return sql, params


def _folder_filter_sql(project_id: str, ordner: str) -> tuple[str, list]:
    """Ordner-Tag-Filter: direkter Elternordner, nur sinnvoll innerhalb eines Projekts
    (ohne Projektfilter wird nach Projekt statt Ordner gruppiert)."""
    if not ordner or not project_id:
        return "", []
    return " AND (dp.path || '/') LIKE ?", [f"%/{ordner}/%"]


def _tag_filter_sql(tag_id: str) -> tuple[str, list]:
    """Globaler Foto-Tag-Filter (ordnerübergreifend, projektunabhängig)."""
    if not tag_id:
        return "", []
    try:
        tid = int(tag_id)
    except ValueError:
        return "", []
    return " AND d.id IN (SELECT document_id FROM photo_tag_assignments WHERE tag_id = ?)", [tid]


def _fetch_photos(
    conn, project_id: str, sterne: str, offset: int, limit: int,
    date_from: str = "", date_to: str = "", formate: str = "", ordner: str = "",
    tag_id: str = "",
) -> list[dict]:
    pf_sql, pf_params = _project_filter_sql(project_id)
    rf_sql, rf_params = _rating_filter_sql(sterne)
    ff_sql, ff_params = _format_filter_sql(formate)
    df_sql, df_params = _date_filter_sql(date_from, date_to)
    of_sql, of_params = _folder_filter_sql(project_id, ordner)
    tf_sql, tf_params = _tag_filter_sql(tag_id)
    placeholders = ",".join("?" * len(_GALLERY_EXTS))
    sql = f"""
        SELECT d.id AS id, d.filename AS filename, d.extension AS extension,
               d.modified_at AS modified_at, d.metadata AS metadata,
               d.project_id AS project_id, p.name AS project_name,
               dp.path AS path, r.rating AS rating
        FROM documents d
        JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        JOIN projects p ON p.id = d.project_id
        LEFT JOIN photo_ratings r ON r.document_id = d.id
        WHERE d.extension IN ({placeholders})
        {pf_sql}
        {rf_sql}
        {ff_sql}
        {df_sql}
        {of_sql}
        {tf_sql}
        ORDER BY COALESCE(json_extract(d.metadata, '$.photo_taken_at'), d.modified_at) DESC, d.id DESC
        LIMIT ? OFFSET ?
    """
    params = (list(_GALLERY_EXTS) + pf_params + rf_params + ff_params + df_params + of_params
              + tf_params + [limit, offset])
    rows = conn.execute(sql, params).fetchall()
    photos = []
    for row in rows:
        d = dict(row)
        d["group"] = _parent_folder_name(d["path"]) if project_id else (d["project_name"] or "")
        photos.append(d)
    return photos


def _fetch_folder_tags(conn, project_id: str) -> list[str]:
    """Distinkte Elternordner-Namen der Fotos im Projekt -- Basis für die dynamischen
    Tag-Filter-Chips. Nur sinnvoll mit gesetztem Projektfilter."""
    if not project_id or project_id.startswith("mailbox:"):
        return []
    pf_sql, pf_params = _project_filter_sql(project_id)
    placeholders = ",".join("?" * len(_GALLERY_EXTS))
    sql = f"""
        SELECT DISTINCT dp.path AS path
        FROM documents d
        JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        WHERE d.extension IN ({placeholders})
        {pf_sql}
    """
    rows = conn.execute(sql, list(_GALLERY_EXTS) + pf_params).fetchall()
    tags = sorted({_parent_folder_name(r["path"]) for r in rows})
    return tags


def _group_photos(photos: list[dict]) -> list[dict]:
    groups: list[dict] = []
    current = None
    for p in photos:
        if current is None or current["name"] != p["group"]:
            current = {"name": p["group"], "photos": []}
            groups.append(current)
        current["photos"].append(p)
    return groups


@router.get("/galerie", response_class=HTMLResponse)
async def galerie(
    request: Request,
    project_id: str = Query(default=""),
    sterne: str = Query(default=""),
    offset: int = Query(default=0),
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
    formate: str = Query(default=""),
    ordner: str = Query(default=""),
    tag_id: str = Query(default=""),
):
    conn = connection.get_connection()
    try:
        photos = _fetch_photos(conn, project_id, sterne, offset, PAGE_SIZE,
                                date_from, date_to, formate, ordner, tag_id)
    finally:
        conn.close()
    groups = _group_photos(photos)
    return templates.TemplateResponse("_gallery_grid.html", {
        "request": request,
        "groups": groups,
        "has_more": len(photos) == PAGE_SIZE,
        "next_offset": offset + PAGE_SIZE,
        "project_id": project_id,
        "sterne": sterne,
        "date_from": date_from,
        "date_to": date_to,
        "formate": formate,
        "ordner": ordner,
        "tag_id": tag_id,
        "is_empty": offset == 0 and not photos,
    })


@router.get("/galerie/kontaktbogen", response_class=HTMLResponse)
async def galerie_kontaktbogen(request: Request, ids: str = Query(default="")):
    """Druckfähiger Kontaktbogen für eine Auswahl von Fotos -- Export läuft über den
    Browser-Druckdialog (@media print), kein serverseitiges PDF-Rendering nötig."""
    id_list = []
    for part in ids.split(","):
        part = part.strip()
        if part.isdigit():
            id_list.append(int(part))

    photos = []
    if id_list:
        conn = connection.get_connection()
        try:
            placeholders = ",".join("?" * len(id_list))
            rows = conn.execute(
                f"""
                SELECT d.id AS id, d.filename AS filename, r.rating AS rating
                FROM documents d
                LEFT JOIN photo_ratings r ON r.document_id = d.id
                WHERE d.id IN ({placeholders})
                """,
                id_list,
            ).fetchall()
        finally:
            conn.close()
        by_id = {row["id"]: dict(row) for row in rows}
        # Auswahlreihenfolge des Nutzers erhalten, nicht die SQL-Rückgabereihenfolge.
        photos = [by_id[i] for i in id_list if i in by_id]

    return templates.TemplateResponse("_kontaktbogen.html", {"request": request, "photos": photos})


@router.get("/galerie/ordner-tags")
async def galerie_ordner_tags(project_id: str = Query(default="")):
    conn = connection.get_connection()
    try:
        tags = _fetch_folder_tags(conn, project_id)
    finally:
        conn.close()
    return JSONResponse({"tags": tags})


@router.get("/foto/{document_id}/thumb")
async def foto_thumb(document_id: int, size: str = Query(default="grid")):
    conn = connection.get_connection()
    try:
        data, content_type = get_thumbnail_bytes(conn, document_id, size)
    finally:
        conn.close()
    return Response(content=data, media_type=content_type,
                     headers={"Cache-Control": "public, max-age=86400"})


@router.get("/foto/{document_id}/ansicht")
async def foto_ansicht(document_id: int):
    conn = connection.get_connection()
    try:
        data, content_type = get_thumbnail_bytes(conn, document_id, "full")
    finally:
        conn.close()
    return Response(content=data, media_type=content_type)


class RatingBody(BaseModel):
    rating: int


@router.post("/foto/{document_id}/bewertung")
async def foto_bewertung(document_id: int, body: RatingBody):
    if body.rating < 0 or body.rating > 5:
        return JSONResponse({"ok": False, "error": "Bewertung muss zwischen 0 und 5 liegen"}, status_code=400)
    conn = connection.get_connection()
    try:
        row = conn.execute("SELECT id FROM documents WHERE id = ?", (document_id,)).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "Dokument nicht gefunden"}, status_code=404)
        queries.set_photo_rating(conn, document_id, body.rating)
    finally:
        conn.close()
    return JSONResponse({"ok": True, "rating": body.rating})


@router.get("/tags/suggest")
async def tags_suggest(q: str = Query(default="")):
    """Autocomplete für die Tag-Eingabemaske (Taste T) -- bestehende Tags, die q
    enthalten, alphabetisch, max. 20."""
    q = q.strip()
    if not q:
        return JSONResponse({"tags": []})
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    conn = connection.get_connection()
    try:
        rows = conn.execute(
            "SELECT id, name FROM photo_tags WHERE name LIKE ? ESCAPE '\\' ORDER BY name COLLATE NOCASE LIMIT 20",
            (f"%{escaped}%",),
        ).fetchall()
    finally:
        conn.close()
    return JSONResponse({"tags": [dict(r) for r in rows]})


@router.get("/galerie/tags")
async def galerie_tags():
    """Alle global vergebenen Tags -- Basis für die Tag-Filter-Chips (nicht
    projektgebunden, im Unterschied zum Ordner-Filter)."""
    conn = connection.get_connection()
    try:
        rows = conn.execute(
            """
            SELECT t.id AS id, t.name AS name
            FROM photo_tags t
            WHERE EXISTS (SELECT 1 FROM photo_tag_assignments a WHERE a.tag_id = t.id)
            ORDER BY t.name COLLATE NOCASE
            """
        ).fetchall()
    finally:
        conn.close()
    return JSONResponse({"tags": [dict(r) for r in rows]})


class TagBody(BaseModel):
    name: str


@router.get("/foto/{document_id}/tags")
async def foto_get_tags(document_id: int):
    conn = connection.get_connection()
    try:
        tags = queries.get_photo_tags(conn, document_id)
    finally:
        conn.close()
    return JSONResponse({"tags": tags})


@router.post("/foto/{document_id}/tags")
async def foto_add_tag(document_id: int, body: TagBody):
    name = body.name.strip()
    if not name:
        return JSONResponse({"ok": False, "error": "Tag-Name fehlt"}, status_code=400)
    conn = connection.get_connection()
    try:
        row = conn.execute("SELECT id FROM documents WHERE id = ?", (document_id,)).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "Dokument nicht gefunden"}, status_code=404)
        tag_id = queries.get_or_create_tag(conn, name)
        queries.assign_photo_tag(conn, document_id, tag_id)
        tags = queries.get_photo_tags(conn, document_id)
    finally:
        conn.close()
    return JSONResponse({"ok": True, "tags": tags})


@router.delete("/foto/{document_id}/tags/{tag_id}")
async def foto_remove_tag(document_id: int, tag_id: int):
    conn = connection.get_connection()
    try:
        queries.remove_photo_tag(conn, document_id, tag_id)
        tags = queries.get_photo_tags(conn, document_id)
    finally:
        conn.close()
    return JSONResponse({"ok": True, "tags": tags})


@router.delete("/tags/{tag_id}")
async def delete_tag_globally(tag_id: int):
    """Löscht einen Tag komplett -- von allen Fotos, ordnerübergreifend. Die
    Zuweisungen (photo_tag_assignments) hängen per ON DELETE CASCADE daran und
    verschwinden automatisch mit."""
    conn = connection.get_connection()
    try:
        conn.execute("DELETE FROM photo_tags WHERE id = ?", (tag_id,))
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"ok": True})
