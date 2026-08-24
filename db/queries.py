from __future__ import annotations

import sqlite3
from typing import Any


def upsert_document(conn: sqlite3.Connection, data: dict[str, Any]) -> int:
    """Insert or ignore a document by hash; return its id."""
    conn.execute(
        """
        INSERT OR IGNORE INTO documents
            (project_id, hash, filename, extension, filesize, modified_at, source_type)
        VALUES
            (:project_id, :hash, :filename, :extension, :filesize, :modified_at, :source_type)
        """,
        data,
    )
    row = conn.execute(
        "SELECT id FROM documents WHERE hash = ?", (data["hash"],)
    ).fetchone()
    return row["id"]


def upsert_path(conn: sqlite3.Connection, document_id: int, path: str, is_primary: bool):
    """Verknüpft einen Pfad mit einem Dokument.

    Wichtig bei GEÄNDERTEN Dateien: ändert sich der Inhalt, entsteht ein neues
    Dokument (neuer Hash). Der Pfad muss dann vom alten auf das neue Dokument
    umgehängt werden — sonst zeigt der Pfad weiter auf die veraltete Version und
    das neue Dokument bleibt ohne Pfad (verwaist, in der Suche nicht öffenbar).
    Frühere Version nutzte INSERT OR IGNORE und hängte nie um.
    """
    prev = conn.execute(
        "SELECT document_id FROM document_paths WHERE path = ?", (path,)
    ).fetchone()

    # Invariante: hoechstens EIN is_primary=1-Pfad pro Dokument. Ohne das kann ein
    # Dokument mit mehreren physischen Kopien (z.B. "_1-500.pdf" und "_1-500-1.pdf",
    # identischer Hash) zwei gleichzeitig als primaer markierte Pfade bekommen -- der
    # LEFT JOIN document_paths ... AND is_primary=1 in der Suche multipliziert dann
    # jede Trefferzeile fuer dieses Dokument (gleiche ID taucht mehrfach mit
    # unterschiedlichem Pfad auf). Vor dem Setzen eines neuen primaeren Pfads werden
    # deshalb alle bisherigen primaeren Pfade desselben Dokuments demotet.
    if is_primary:
        conn.execute(
            "UPDATE document_paths SET is_primary = 0 "
            "WHERE document_id = ? AND path != ? AND is_primary = 1",
            (document_id, path),
        )

    conn.execute(
        """
        INSERT INTO document_paths (document_id, path, is_primary)
        VALUES (?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            document_id = excluded.document_id,
            is_primary  = excluded.is_primary
        """,
        (document_id, path, int(is_primary)),
    )

    # Alte Dokument-Version aufräumen, wenn ihr letzter Pfad gerade umgehängt wurde.
    # Dokumente die noch andere Pfade haben (echte Duplikate) bleiben erhalten.
    if prev and prev["document_id"] != document_id:
        old_id = prev["document_id"]
        still_referenced = conn.execute(
            "SELECT 1 FROM document_paths WHERE document_id = ? LIMIT 1", (old_id,)
        ).fetchone()
        if not still_referenced:
            # CASCADE entfernt content/chunks; Trigger documents_fts_doc_delete den FTS-Eintrag
            conn.execute("DELETE FROM documents WHERE id = ?", (old_id,))


def set_extraction_status(conn: sqlite3.Connection, document_id: int, status: str):
    conn.execute(
        "UPDATE documents SET extraction_status = ? WHERE id = ?",
        (status, document_id),
    )


def update_metadata(conn: sqlite3.Connection, document_id: int, meta: dict):
    import json
    row = conn.execute("SELECT metadata FROM documents WHERE id = ?", (document_id,)).fetchone()
    if not row:
        return
    try:
        current = json.loads(row["metadata"] or "{}")
    except Exception:
        current = {}
    current.update(meta)
    conn.execute("UPDATE documents SET metadata = ? WHERE id = ?",
                 (json.dumps(current), document_id))


def upsert_content(conn: sqlite3.Connection, document_id: int, content: str, language: str = ""):
    conn.execute(
        """
        INSERT INTO document_content (document_id, content, language)
        VALUES (?, ?, ?)
        ON CONFLICT(document_id) DO UPDATE SET content = excluded.content, language = excluded.language
        """,
        (document_id, content, language),
    )


def save_chunks(conn: sqlite3.Connection, document_id: int, chunks: list[dict]):
    """Bestehende Chunks löschen und neu einfügen."""
    conn.execute("DELETE FROM document_chunks WHERE document_id = ?", (document_id,))
    conn.executemany(
        "INSERT INTO document_chunks (document_id, page_number, chunk_index, content) VALUES (?, ?, ?, ?)",
        [(document_id, c["page_number"], c["chunk_index"], c["content"]) for c in chunks],
    )


def get_photo_rating(conn: sqlite3.Connection, document_id: int) -> int:
    row = conn.execute(
        "SELECT rating FROM photo_ratings WHERE document_id = ?", (document_id,)
    ).fetchone()
    return row["rating"] if row else 0


def set_photo_rating(conn: sqlite3.Connection, document_id: int, rating: int) -> None:
    """rating 0 entfernt die Bewertung (keine Zeile = unbewertet)."""
    if rating <= 0:
        conn.execute("DELETE FROM photo_ratings WHERE document_id = ?", (document_id,))
    else:
        rating = min(rating, 5)
        conn.execute(
            """
            INSERT INTO photo_ratings (document_id, rating, rated_at)
            VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            ON CONFLICT(document_id) DO UPDATE SET
                rating   = excluded.rating,
                rated_at = excluded.rated_at
            """,
            (document_id, rating),
        )
    conn.commit()


def get_or_create_tag(conn: sqlite3.Connection, name: str) -> int:
    name = name.strip()
    conn.execute("INSERT OR IGNORE INTO photo_tags (name) VALUES (?)", (name,))
    row = conn.execute(
        "SELECT id FROM photo_tags WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    return row["id"]


def assign_photo_tag(conn: sqlite3.Connection, document_id: int, tag_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO photo_tag_assignments (document_id, tag_id) VALUES (?, ?)",
        (document_id, tag_id),
    )
    conn.commit()


def remove_photo_tag(conn: sqlite3.Connection, document_id: int, tag_id: int) -> None:
    conn.execute(
        "DELETE FROM photo_tag_assignments WHERE document_id = ? AND tag_id = ?",
        (document_id, tag_id),
    )
    conn.commit()


def rename_tag(conn: sqlite3.Connection, tag_id: int, new_name: str) -> bool:
    """Benennt einen Tag um. Gibt False zurück, wenn der neue Name bereits (case-
    insensitiv) einem ANDEREN Tag gehört -- UNIQUE COLLATE NOCASE auf photo_tags.name
    würde das sonst als IntegrityError werfen statt eine sinnvolle Fehlermeldung
    zuzulassen."""
    new_name = new_name.strip()
    if not new_name:
        return False
    clash = conn.execute(
        "SELECT id FROM photo_tags WHERE name = ? COLLATE NOCASE AND id != ?",
        (new_name, tag_id),
    ).fetchone()
    if clash:
        return False
    conn.execute("UPDATE photo_tags SET name = ? WHERE id = ?", (new_name, tag_id))
    conn.commit()
    return True


def get_photo_tags(conn: sqlite3.Connection, document_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT t.id AS id, t.name AS name
        FROM photo_tags t
        JOIN photo_tag_assignments a ON a.tag_id = t.id
        WHERE a.document_id = ?
        ORDER BY t.name COLLATE NOCASE
        """,
        (document_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_project_by_path(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM projects WHERE path = ?", (path,)
    ).fetchone()


def insert_project(conn: sqlite3.Connection, name: str, path: str) -> int:
    cur = conn.execute(
        "INSERT OR IGNORE INTO projects (name, path) VALUES (?, ?)", (name, path)
    )
    if cur.lastrowid:
        return cur.lastrowid
    return conn.execute(
        "SELECT id FROM projects WHERE path = ?", (path,)
    ).fetchone()["id"]
