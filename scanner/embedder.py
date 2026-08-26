"""Embedding-Generierung und Vektor-Suche via Ollama (lokal)."""
from __future__ import annotations

import logging
import shutil
import sqlite3
import numpy as np
import httpx
from pathlib import Path

log = logging.getLogger(__name__)

OLLAMA_URL  = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"
LLM_MODEL   = "llama3.2:3b"


# ── Verfügbarkeit ─────────────────────────────────────────────────────────────

def is_ollama_installed() -> bool:
    return bool(
        shutil.which("ollama") or
        Path("/usr/local/bin/ollama").exists() or
        Path("/opt/homebrew/bin/ollama").exists()
    )


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
    if not is_ollama_installed():
        return {"ok": False, "ollama_missing": True,
                "reason": "Ollama nicht installiert"}
    if not is_ollama_running():
        return {"ok": False, "ollama_missing": False,
                "reason": "Ollama läuft nicht"}
    if not model_available(EMBED_MODEL):
        return {"ok": False, "ollama_missing": False,
                "reason": f"Modell {EMBED_MODEL} nicht geladen"}
    if not model_available(LLM_MODEL):
        return {"ok": False, "ollama_missing": False,
                "reason": f"Modell {LLM_MODEL} nicht geladen"}
    return {"ok": True, "ollama_missing": False, "reason": ""}


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

EMBED_BATCH_SIZE = 20   # Chunks pro Ollama-Request
EMBED_MAX_CHARS  = 2000  # nomic-embed-text: sicher unter 512 Token (dt. Text ~4 Zeichen/Token)


def embed_document_chunks(conn: sqlite3.Connection, document_id: int) -> int:
    """Berechnet Embeddings für alle Chunks eines Dokuments in Batches. Gibt Anzahl zurück."""
    rows = conn.execute(
        "SELECT id, content FROM document_chunks WHERE document_id = ? ORDER BY chunk_index",
        (document_id,)
    ).fetchall()
    if not rows:
        return 0
    total = 0
    # In kleinen Batches einbetten — verhindert Timeout bei grossen Dokumenten
    for i in range(0, len(rows), EMBED_BATCH_SIZE):
        batch = rows[i:i + EMBED_BATCH_SIZE]
        texts = [(r["content"] or "")[:EMBED_MAX_CHARS] for r in batch]
        vecs  = embed_texts(texts)
        if vecs is None:
            log.warning("Embedding fehlgeschlagen für doc %s batch %d", document_id, i)
            continue
        with conn:
            for row, vec in zip(batch, vecs):
                conn.execute(
                    "UPDATE document_chunks SET embedding = ? WHERE id = ?",
                    (vec.tobytes(), row["id"])
                )
        total += len(batch)
    return total


# ── Vektor-Suche ──────────────────────────────────────────────────────────────

def vector_search(
    conn: sqlite3.Connection,
    query_vec: np.ndarray,
    project_id: str = "",
    limit: int = 8,
    extra_filter_sql: str = "",
    extra_filter_params: list | None = None,
) -> list[dict]:
    """Findet die ähnlichsten Chunks per Cosine-Similarity.
    extra_filter_sql/extra_filter_params: zusätzliche SQL-Bedingung (z.B. Typ-Filter
    "Nur Mail" aus der Such-UI) -- muss mit " AND ..." beginnen. Ohne das würde ein in
    der Such-UI gesetzter Typ-Filter von der KI-Suche stillschweigend ignoriert."""
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
    """ + extra_filter_sql
    extra_params = extra_filter_params or []
    if project_id:
        try:
            rows = conn.execute(base_sql + " AND d.project_id = ?",
                                extra_params + [int(project_id)]).fetchall()
        except (ValueError, TypeError):
            rows = []
    else:
        rows = conn.execute(base_sql, extra_params).fetchall()

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
        r["match_type"] = "semantic"
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


def _de_umlaut(s: str) -> str:
    """Umlaute für robuste Suche entfernen: ä→a, ö→o, ü→u, ß→ss."""
    return (s.replace("ä", "a").replace("ö", "o").replace("ü", "u")
             .replace("Ä", "A").replace("Ö", "O").replace("Ü", "U")
             .replace("ß", "ss"))


_CHUNK_SELECT = """
    SELECT dc.id, dc.document_id, dc.chunk_index, dc.content, dc.page_number,
           d.filename, d.extension, d.project_id,
           dp.path AS filepath, p.name AS project_name,
           {score} AS score, '{match_type}' AS match_type
    FROM {from_clause}
    JOIN documents d ON d.id = dc.document_id
    JOIN projects  p ON p.id = d.project_id
    LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
"""


def keyword_search_chunks(
    conn: sqlite3.Connection,
    query: str,
    project_id: str = "",
    limit: int = 4,
    extra_filter_sql: str = "",
    extra_filter_params: list | None = None,
) -> list[dict]:
    """Keyword-Suche in Chunks. Drei Strategien, nach Relevanz gemergt:
    1. FTS5 BM25 (primär, umlaut-sicher, gerankt)
    2. LIKE mit Umlaut-Normalisierung (fallback, umlaut-sicher)
    3. Heading-Suche (UPPERCASE-Term = SIA-Norm-Definitions-Überschrift, boost)

    extra_filter_sql/extra_filter_params: zusätzliche SQL-Bedingung (z.B. Typ-Filter
    "Nur Mail" aus der Such-UI) -- muss mit " AND ..." beginnen. Ohne das würde ein in
    der Such-UI gesetzter Typ-Filter von der KI-Suche stillschweigend ignoriert."""
    import re
    words = [re.sub(r'["\(\)\*\:\^]', "", w) for w in query.lower().split() if w]
    words = [w for w in words if len(w) > 2 and w not in _STOPWORDS]
    if not words:
        return []

    proj_clause = extra_filter_sql
    proj_params: list = list(extra_filter_params or [])
    if project_id:
        try:
            proj_clause += " AND d.project_id = ?"
            proj_params.append(int(project_id))
        except (ValueError, TypeError):
            pass

    # ── Strategie 1: FTS5 mit BM25-Ranking ───────────────────────────────────
    # chunks_fts verwendet tokenize='unicode61 remove_diacritics 2':
    # Umlaute werden beim Indexieren UND beim Suchen entfernt → robust.
    # Wir senden sowohl "geschossfläche*" als auch "geschossflache*" (ASCII-Fallback).
    fts_terms = []
    for w in words:
        fts_terms.append(f"{w}*")
        w_a = _de_umlaut(w)
        if w_a != w:
            fts_terms.append(f"{w_a}*")
    fts_q = " OR ".join(fts_terms)
    fts_rows: list = []
    try:
        fts_rows = conn.execute(
            _CHUNK_SELECT.format(score="0.82", match_type="fts", from_clause="chunks_fts JOIN document_chunks dc ON chunks_fts.rowid = dc.id") +
            f" WHERE chunks_fts MATCH ? {proj_clause} ORDER BY rank LIMIT ?",
            [fts_q] + proj_params + [limit * 3]
        ).fetchall()
    except Exception as e:
        log.warning("FTS-Chunk-Suche fehlgeschlagen: %s", e)

    # ── Strategie 2: LIKE mit Umlaut-Normalisierung (SQLite LOWER + replace) ─
    # SQLite LOWER() normalisiert keine Umlaute (Ä bleibt Ä).
    # Lösung: replace() für Umlaute auf beiden Seiten.
    def _norm_sql_expr(col: str) -> str:
        return (f"replace(replace(replace(replace(replace(replace("
                f"lower({col}),"
                f"'ä','a'),'ö','o'),'ü','u'),'ß','ss'),'Ä','a'),'Ö','o')")

    norm_words = [_de_umlaut(w) for w in words]

    # AND-Suche: alle Wörter müssen vorkommen (präzise, aber eng)
    like_clauses_and = " AND ".join(f"{_norm_sql_expr('dc.content')} LIKE ?" for _ in norm_words)
    like_rows: list = []
    try:
        like_rows = conn.execute(
            _CHUNK_SELECT.format(score="0.72", match_type="like_and", from_clause="document_chunks dc") +
            f" WHERE {like_clauses_and} {proj_clause} ORDER BY length(dc.content) LIMIT ?",
            [f"%{w}%" for w in norm_words] + proj_params + [limit * 2]
        ).fetchall()
    except Exception as e:
        log.warning("LIKE-AND-Suche fehlgeschlagen: %s", e)

    # OR-Suche: mindestens ein Wort (breiter Recall, für generische/kurze Queries)
    # Nur die längsten Wörter nehmen (> 6 Zeichen) um False-Positives zu reduzieren
    long_words = [w for w in norm_words if len(w) > 6]
    like_or_rows: list = []
    if long_words and len(like_rows) < limit:
        like_clauses_or = " OR ".join(f"{_norm_sql_expr('dc.content')} LIKE ?" for _ in long_words)
        try:
            like_or_rows = conn.execute(
                _CHUNK_SELECT.format(score="0.60", match_type="like_or", from_clause="document_chunks dc") +
                f" WHERE ({like_clauses_or}) {proj_clause} ORDER BY length(dc.content) LIMIT ?",
                [f"%{w}%" for w in long_words] + proj_params + [limit]
            ).fetchall()
        except Exception as e:
            log.warning("LIKE-OR-Suche fehlgeschlagen: %s", e)

    # ── Strategie 3: Heading-Boost ────────────────────────────────────────────
    # SIA-Normen kennzeichnen Definitionen mit GROSSBUCHSTABEN: "2 GESCHOSSFLÄCHE GF"
    # Treffer hier bekommen höchsten Score → kommen als erste ins Ergebnis.
    heading_rows: list = []
    try:
        for w in words:
            for variant in {w.upper(), _de_umlaut(w).upper()}:
                heading_rows += conn.execute(
                    _CHUNK_SELECT.format(score="0.90", match_type="heading", from_clause="document_chunks dc") +
                    f" WHERE INSTR(dc.content, ?) > 0 {proj_clause}"
                    f" ORDER BY length(dc.content) LIMIT ?",
                    [variant] + proj_params + [limit]
                ).fetchall()
    except Exception as e:
        log.warning("Heading-Suche fehlgeschlagen: %s", e)

    # ── Merge: Heading → FTS → LIKE-AND → LIKE-OR ────────────────────────────
    seen   = set()
    merged = []
    for row in list(heading_rows) + list(fts_rows) + list(like_rows) + list(like_or_rows):
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
