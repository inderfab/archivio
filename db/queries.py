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
    conn.execute(
        """
        INSERT OR IGNORE INTO document_paths (document_id, path, is_primary)
        VALUES (?, ?, ?)
        """,
        (document_id, path, int(is_primary)),
    )


def set_extraction_status(conn: sqlite3.Connection, document_id: int, status: str):
    conn.execute(
        "UPDATE documents SET extraction_status = ? WHERE id = ?",
        (status, document_id),
    )


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
