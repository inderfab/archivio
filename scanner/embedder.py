"""Embedding-Generierung und Vektor-Suche via Ollama (lokal)."""
from __future__ import annotations

import logging
import sqlite3
import numpy as np
import httpx

log = logging.getLogger(__name__)

OLLAMA_URL  = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
LLM_MODEL   = "llama3.2:3b"


# ── Verfügbarkeit ─────────────────────────────────────────────────────────────

def is_ollama_running() -> bool:
    try:
        httpx.get(f"{OLLAMA_URL}/", timeout=2)
        return True
    except Exception:
        return False


def model_available(model: str) -> bool:
    try:
        resp = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        models = [m["name"].split(":")[0] for m in resp.json().get("models", [])]
        return model.split(":")[0] in models
    except Exception:
        return False


def ai_status() -> dict:
    if not is_ollama_running():
        return {"ok": False, "reason": "Ollama läuft nicht"}
    if not model_available(EMBED_MODEL):
        return {"ok": False, "reason": f"Modell {EMBED_MODEL} nicht geladen"}
    if not model_available(LLM_MODEL):
        return {"ok": False, "reason": f"Modell {LLM_MODEL} nicht geladen"}
    return {"ok": True, "reason": ""}


# ── Embeddings ────────────────────────────────────────────────────────────────

def embed_texts(texts: list[str]) -> np.ndarray | None:
    """Gibt (N, D) float32-Array zurück oder None bei Fehler."""
    if not texts:
        return None
    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": EMBED_MODEL, "input": texts},
            timeout=120,
        )
        resp.raise_for_status()
        vecs = resp.json().get("embeddings")
        if not vecs:
            return None
        arr = np.array(vecs, dtype=np.float32)
        # L2-Normalisierung für Cosine-Similarity via Dot-Product
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1
        return arr / norms
    except Exception as e:
        log.warning("Embedding fehlgeschlagen: %s", e)
        return None


def embed_query(text: str) -> np.ndarray | None:
    result = embed_texts([text])
    return result[0] if result is not None else None


# ── Chunks einbetten und speichern ────────────────────────────────────────────

def embed_document_chunks(conn: sqlite3.Connection, document_id: int) -> int:
    """Berechnet Embeddings für alle Chunks eines Dokuments. Gibt Anzahl zurück."""
    rows = conn.execute(
        "SELECT id, content FROM document_chunks WHERE document_id = ? ORDER BY chunk_index",
        (document_id,)
    ).fetchall()
    if not rows:
        return 0
    texts = [r["content"] or "" for r in rows]
    vecs  = embed_texts(texts)
    if vecs is None:
        return 0
    with conn:
        for row, vec in zip(rows, vecs):
            conn.execute(
                "UPDATE document_chunks SET embedding = ? WHERE id = ?",
                (vec.tobytes(), row["id"])
            )
    return len(rows)


# ── Vektor-Suche ──────────────────────────────────────────────────────────────

def vector_search(
    conn: sqlite3.Connection,
    query_vec: np.ndarray,
    project_id: str = "",
    limit: int = 8,
) -> list[dict]:
    """Findet die ähnlichsten Chunks per Cosine-Similarity."""
    base_sql = """
        SELECT dc.id, dc.document_id, dc.chunk_index, dc.content, dc.page_number,
               dc.embedding,
               d.filename, d.extension, d.project_id,
               dp.path  AS filepath,
               p.name   AS project_name
        FROM document_chunks dc
        JOIN documents d        ON d.id  = dc.document_id
        JOIN projects  p        ON p.id  = d.project_id
        LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        WHERE dc.embedding IS NOT NULL
    """
    if project_id:
        try:
            rows = conn.execute(base_sql + " AND d.project_id = ?",
                                (int(project_id),)).fetchall()
        except (ValueError, TypeError):
            rows = []
    else:
        rows = conn.execute(base_sql).fetchall()

    if not rows:
        return []

    embeddings = np.array(
        [np.frombuffer(r["embedding"], dtype=np.float32) for r in rows]
    )
    scores   = embeddings @ query_vec          # cosine similarity (vecs already normalised)
    top_idx  = np.argsort(scores)[::-1][:limit]

    # Für jeden Treffer den nächsten Chunk holen (Kontext über Chunk-Grenzen hinweg)
    chunk_ids = [rows[i]["id"] for i in top_idx]
    if chunk_ids:
        placeholders = ",".join("?" * len(chunk_ids))
        next_chunks = {
            r[0]: r[1]
            for r in conn.execute(f"""
                SELECT dc_cur.id, dc_nxt.content
                FROM document_chunks dc_cur
                JOIN document_chunks dc_nxt
                  ON dc_nxt.document_id = dc_cur.document_id
                 AND dc_nxt.chunk_index  = dc_cur.chunk_index + 1
                WHERE dc_cur.id IN ({placeholders})
            """, chunk_ids).fetchall()
        }
    else:
        next_chunks = {}

    results = []
    for i in top_idx:
        r = dict(rows[i])
        r["score"] = float(scores[i])
        r.pop("embedding", None)
        if r["id"] in next_chunks:
            r["content"] = r["content"].rstrip() + "\n" + next_chunks[r["id"]]
        results.append(r)
    return results


# ── Keyword-Suche in Chunks (FTS) ────────────────────────────────────────────

_STOPWORDS = {
    "bis", "wann", "muss", "man", "das", "die", "der", "den", "dem",
    "ein", "eine", "und", "oder", "für", "mit", "von", "zu", "auf",
    "ist", "sind", "hat", "wird", "war", "ich", "sie", "wir", "ihr",
    "wie", "was", "wer", "wo", "welche", "auch", "noch", "nicht",
    "gibt", "gibt", "kann", "alle", "beim", "nach", "als",
}


def keyword_search_chunks(
    conn: sqlite3.Connection,
    query: str,
    project_id: str = "",
    limit: int = 4,
) -> list[dict]:
    """Hybrid-Keyword-Suche: LIKE-AND (morphologisch) + FTS-OR (Recall)."""
    import re
    words = [re.sub(r'["\(\)\*\:\^]', "", w) for w in query.lower().split() if w]
    words = [w for w in words if len(w) > 2 and w not in _STOPWORDS]
    if not words:
        return []

    proj_clause = ""
    proj_params: list = []
    if project_id:
        try:
            proj_clause = "AND d.project_id = ?"
            proj_params.append(int(project_id))
        except (ValueError, TypeError):
            pass

    # ── Strategie 1: LIKE AND mit 3-Zeichen-Präfix (deckt dt. Morphologie ab) ──
    prefixes = [w[:3] for w in words]
    like_clauses = " AND ".join(f"LOWER(dc.content) LIKE ?" for _ in prefixes)
    like_params   = [f"%{p}%" for p in prefixes] + proj_params + [limit * 2]
    like_rows: list = []
    try:
        like_rows = conn.execute(f"""
            SELECT dc.id, dc.document_id, dc.chunk_index, dc.content, dc.page_number,
                   d.filename, d.extension, d.project_id,
                   dp.path AS filepath, p.name AS project_name,
                   0.95 AS score
            FROM document_chunks dc
            JOIN documents d ON d.id = dc.document_id
            JOIN projects  p ON p.id = d.project_id
            LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
            WHERE {like_clauses} {proj_clause}
            LIMIT ?
        """, like_params).fetchall()
    except Exception as e:
        log.warning("LIKE-Chunk-Suche fehlgeschlagen: %s", e)

    # ── Strategie 2: FTS OR (breiter Recall für nicht-morphologische Fälle) ──
    fts_q    = " OR ".join(f"{w}*" for w in words)
    fts_rows: list = []
    try:
        fts_rows = conn.execute(f"""
            SELECT dc.id, dc.document_id, dc.chunk_index, dc.content, dc.page_number,
                   d.filename, d.extension, d.project_id,
                   dp.path AS filepath, p.name AS project_name,
                   0.90 AS score
            FROM chunks_fts
            JOIN document_chunks dc ON chunks_fts.rowid = dc.id
            JOIN documents d        ON d.id = dc.document_id
            JOIN projects  p        ON p.id = d.project_id
            LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
            WHERE chunks_fts MATCH ? {proj_clause}
            ORDER BY rank
            LIMIT ?
        """, [fts_q] + proj_params + [limit * 2]).fetchall()
    except Exception as e:
        log.warning("FTS-Chunk-Suche fehlgeschlagen: %s", e)

    # ── Merge: LIKE zuerst (präziser), dann FTS-Ergänzungen ──
    seen   = set()
    merged = []
    for row in list(like_rows) + list(fts_rows):
        if row["id"] not in seen:
            seen.add(row["id"])
            merged.append(row)
        if len(merged) >= limit:
            break

    # ── Folge-Chunk anhängen für vollständigen Kontext ──
    chunk_ids = [r["id"] for r in merged]
    next_chunks: dict = {}
    if chunk_ids:
        placeholders = ",".join("?" * len(chunk_ids))
        next_chunks = {
            r[0]: r[1]
            for r in conn.execute(f"""
                SELECT dc_cur.id, dc_nxt.content
                FROM document_chunks dc_cur
                JOIN document_chunks dc_nxt
                  ON dc_nxt.document_id = dc_cur.document_id
                 AND dc_nxt.chunk_index  = dc_cur.chunk_index + 1
                WHERE dc_cur.id IN ({placeholders})
            """, chunk_ids).fetchall()
        }

    results = []
    for row in merged:
        r = dict(row)
        if r["id"] in next_chunks:
            r["content"] = r["content"].rstrip() + "\n" + next_chunks[r["id"]]
        results.append(r)
    return results


# ── LLM-Antwort ───────────────────────────────────────────────────────────────

def llm_answer(question: str, chunks: list[dict]) -> str:
    context = "\n\n---\n\n".join(
        f"[{c['filename']}]\n{c['content']}"
        for c in chunks
    )
    prompt = (
        "Du bist ein präziser Assistent für ein Schweizer Architekturbüro.\n"
        "Beantworte AUSSCHLIESSLICH die folgende Frage in 1–3 klaren Sätzen.\n"
        "Stütze dich NUR auf die unten stehenden Dokumentenausschnitte.\n"
        "Erfinde keine Fragen und gib keine weiteren Erklärungen.\n"
        "Wenn die Antwort nicht im Kontext steht, antworte: "
        "«Diese Information ist in den Dokumenten nicht vorhanden.»\n\n"
        f"Dokumentenausschnitte:\n{context}\n\n"
        f"Frage: {question}\n\n"
        "Antwort (kurz und direkt):"
    )
    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model":   LLM_MODEL,
                "prompt":  prompt,
                "stream":  False,
                "options": {"temperature": 0.1, "num_predict": 300},
            },
            timeout=180,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as e:
        log.error("LLM-Fehler: %s", e)
        return f"Fehler beim Generieren der Antwort: {e}"
