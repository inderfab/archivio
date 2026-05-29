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
    if project_id:
        try:
            rows = conn.execute("""
                SELECT dc.id, dc.content, dc.page_number,
                       dc.embedding,
                       d.filename, d.extension, d.project_id,
                       dp.path  AS filepath,
                       p.name   AS project_name
                FROM document_chunks dc
                JOIN documents d        ON d.id  = dc.document_id
                JOIN projects  p        ON p.id  = d.project_id
                LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
                WHERE dc.embedding IS NOT NULL AND d.project_id = ?
            """, (int(project_id),)).fetchall()
        except (ValueError, TypeError):
            rows = []
    else:
        rows = conn.execute("""
            SELECT dc.id, dc.content, dc.page_number,
                   dc.embedding,
                   d.filename, d.extension, d.project_id,
                   dp.path  AS filepath,
                   p.name   AS project_name
            FROM document_chunks dc
            JOIN documents d        ON d.id  = dc.document_id
            JOIN projects  p        ON p.id  = d.project_id
            LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
            WHERE dc.embedding IS NOT NULL
        """).fetchall()

    if not rows:
        return []

    embeddings = np.array(
        [np.frombuffer(r["embedding"], dtype=np.float32) for r in rows]
    )
    scores   = embeddings @ query_vec          # cosine similarity (vecs already normalised)
    top_idx  = np.argsort(scores)[::-1][:limit]

    results = []
    for i in top_idx:
        r = dict(rows[i])
        r["score"] = float(scores[i])
        r.pop("embedding", None)
        results.append(r)
    return results


# ── LLM-Antwort ───────────────────────────────────────────────────────────────

def llm_answer(question: str, chunks: list[dict]) -> str:
    context = "\n\n---\n\n".join(
        f"[{c['filename']}, Seite {c.get('page_number') or '?'}]\n{c['content']}"
        for c in chunks
    )
    prompt = (
        "Du bist ein präziser Assistent für ein Schweizer Architekturbüro. "
        "Beantworte die Frage ausschliesslich anhand der folgenden Dokumentenausschnitte. "
        "Wenn die Information nicht eindeutig vorhanden ist, sage das klar. "
        "Antworte auf Deutsch.\n\n"
        f"Kontext:\n{context}\n\n"
        f"Frage: {question}\n\n"
        "Antwort:"
    )
    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": LLM_MODEL, "prompt": prompt, "stream": False},
            timeout=180,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as e:
        log.error("LLM-Fehler: %s", e)
        return f"Fehler beim Generieren der Antwort: {e}"
