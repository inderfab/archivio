"""
Recall-Tests für die Volltextsuche gegen eine echte Datenbank.

Idee (vom Nutzer): Zufälliges PDF und Seite auswählen, Textausschnitt kopieren, damit
suchen — der Text muss im Quelldokument gefunden werden.

Gemessen wird bewusst _run_scoped_search() aus web/main.py, also derselbe Weg, den die
Suchseite und /api/mcp/search gehen. Bis Server 3.4.4 lief dieser Test über
scanner.embedder.keyword_search_chunks(), eine zweite Implementierung, die kein
Kundenpfad ausführte -- die Zahlen sagten damit nichts über die echte Suche aus.

Ausführen (läuft nicht in der normalen Suite, braucht eine befüllte Datenbank):
  ARCHIVIO_DB=<pfad-zur-archivio.db> pytest tests/test_search_recall.py -v
"""
from __future__ import annotations

import os
import random
import sqlite3
import sys
from pathlib import Path

import pytest

# Projektpfad einbinden
sys.path.insert(0, str(Path(__file__).parent.parent))

# ── DB-Fixture ────────────────────────────────────────────────────────────────

def _get_db_path() -> Path | None:
    """Gibt den Pfad zur echten DB zurück, falls vorhanden."""
    # 1. Env-Variable
    if env := os.environ.get("ARCHIVIO_DB"):
        p = Path(env)
        if p.exists():
            return p
    # 2. Standard-Installationspfad (iMac/lokaler Rechner)
    candidates = [
        Path.home() / "Library" / "Application Support" / "Archivio" / "archivio.db",
        Path(__file__).parent.parent / "archivio.db",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


@pytest.fixture(scope="module")
def live_db():
    """SQLite-Verbindung zur echten Archivio-DB (read-only)."""
    db_path = _get_db_path()
    if db_path is None:
        pytest.skip("Keine Archivio-DB gefunden. ARCHIVIO_DB=<pfad> setzen.")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def sample_chunks(live_db):
    """Wählt N zufällige Chunks mit sinnvollem Inhalt aus der DB."""
    rows = live_db.execute("""
        SELECT dc.id, dc.document_id, dc.content, dc.chunk_index,
               d.filename, d.extension
        FROM document_chunks dc
        JOIN documents d ON d.id = dc.document_id
        WHERE length(dc.content) >= 150
          AND d.extraction_status = 'ok'
          AND d.extension IN ('.pdf', '.docx', '.txt', '.doc')
        ORDER BY RANDOM()
        LIMIT 20
    """).fetchall()
    if not rows:
        pytest.skip("Keine geeigneten Chunks in der DB.")
    return [dict(r) for r in rows]


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def _excerpt(content: str, length: int = 80) -> str:
    """Nimmt einen Ausschnitt aus der Mitte des Textes (realistischer als Anfang/Ende)."""
    text = content.strip()
    if len(text) <= length:
        return text
    # Mitte des Textes, an Wortgrenze schneiden
    mid = len(text) // 2
    start = max(0, mid - length // 2)
    snippet = text[start:start + length]
    # An Wortgrenze trimmen
    if " " in snippet[5:]:
        snippet = snippet[snippet.index(" ", 5):]
    return snippet.strip()


def _suche(conn, query: str, limit: int = 10) -> list[dict]:
    """Die produktive Volltextsuche -- derselbe Einstieg wie /search."""
    from web.main import _run_scoped_search
    treffer, _fehler = _run_scoped_search(conn, query, "", [], {"docs"})
    return treffer[:limit]


def _doc_in_results(results: list[dict], document_id: int) -> bool:
    return any(r.get("id") == document_id for r in results)


# ── 1. Keyword-Recall ─────────────────────────────────────────────────────────

class TestVolltextRecall:
    """Die Suche muss das Quelldokument finden, wenn man Text daraus sucht."""

    @pytest.mark.parametrize("n_samples,min_hits", [(10, 7)])
    def test_recall_rate(self, live_db, sample_chunks, n_samples, min_hits):
        """Mindestens min_hits von n_samples Chunks müssen gefunden werden."""
        chunks = random.sample(sample_chunks, min(n_samples, len(sample_chunks)))
        hits = 0
        misses = []

        for chunk in chunks:
            query = _excerpt(chunk["content"], length=60)
            results = _suche(live_db, query, limit=10)
            if _doc_in_results(results, chunk["document_id"]):
                hits += 1
            else:
                misses.append({
                    "filename": chunk["filename"],
                    "query":    query[:50] + "…",
                    "got":      [r.get("filename") for r in results[:3]],
                })

        recall_pct = hits / len(chunks) * 100
        print(f"\nVolltext-Recall: {hits}/{len(chunks)} = {recall_pct:.0f}%")
        if misses:
            print("Misses:")
            for m in misses:
                print(f"  ✗ {m['filename'][:40]} | query: {m['query']}")
                print(f"    Stattdessen: {m['got']}")

        assert hits >= min_hits, (
            f"Recall zu niedrig: {hits}/{len(chunks)} ({recall_pct:.0f}%) "
            f"— Minimum: {min_hits}/{len(chunks)}"
        )

    def test_exact_chunk_found(self, live_db, sample_chunks):
        """Einzelner Test: langer Excerpt muss exakt das richtige Dokument finden."""
        chunk = sample_chunks[0]
        # Längerer Excerpt = präziser
        query = _excerpt(chunk["content"], length=120)
        results = _suche(live_db, query, limit=5)

        assert _doc_in_results(results, chunk["document_id"]), (
            f"Dokument '{chunk['filename']}' nicht in Resultaten.\n"
            f"Query: {query[:80]}\n"
            f"Gefunden: {[r.get('filename') for r in results]}"
        )

    def test_short_query_still_finds(self, live_db, sample_chunks):
        """Auch kurze Queries (30 Zeichen) müssen funktionieren."""
        found_count = 0
        for chunk in sample_chunks[:5]:
            query = _excerpt(chunk["content"], length=30)
            if len(query) < 10:
                continue
            results = _suche(live_db, query, limit=10)
            if _doc_in_results(results, chunk["document_id"]):
                found_count += 1

        assert found_count >= 2, (
            f"Kurze Queries finden zu wenig: {found_count}/5"
        )
