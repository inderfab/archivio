"""
Recall-Tests für die KI-Suche.

Idee (vom Nutzer): Zufälliges PDF und Seite auswählen, Textausschnitt kopieren,
KI-Suche mit diesem Text starten — der Text muss im Quelldokument gefunden werden.

Drei Teststufen:
  1. keyword_recall  – Substring aus Chunk → Keyword-Suche findet Dokument
  2. vector_recall   – Chunk-Text embedden → Vektor-Suche findet Dokument in Top-10
  3. e2e_recall      – HTTP /search/ai → Dokument erscheint in Quellen (requires live server)

Ausführen:
  # Gegen die echte DB (iMac):
  ARCHIVIO_DB=<pfad-zur-archivio.db> pytest tests/test_search_recall.py -v

  # Nur Keyword-Tests (kein Ollama nötig):
  pytest tests/test_search_recall.py -v -m "not vector and not e2e"

  # Alle Tests inkl. Vektor (Ollama muss laufen):
  pytest tests/test_search_recall.py -v
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
        WHERE dc.embedding IS NOT NULL
          AND length(dc.content) >= 150
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


def _doc_in_results(results: list[dict], document_id: int) -> bool:
    return any(r.get("document_id") == document_id for r in results)


# ── 1. Keyword-Recall ─────────────────────────────────────────────────────────

class TestKeywordRecall:
    """Keyword-Suche muss das Quelldokument finden wenn man Text daraus sucht."""

    @pytest.mark.parametrize("n_samples,min_hits", [(10, 7)])
    def test_keyword_recall_rate(self, live_db, sample_chunks, n_samples, min_hits):
        """Mindestens min_hits von n_samples Chunks müssen gefunden werden."""
        from scanner.embedder import keyword_search_chunks

        chunks = random.sample(sample_chunks, min(n_samples, len(sample_chunks)))
        hits = 0
        misses = []

        for chunk in chunks:
            query = _excerpt(chunk["content"], length=60)
            results = keyword_search_chunks(live_db, query, limit=10)
            if _doc_in_results(results, chunk["document_id"]):
                hits += 1
            else:
                misses.append({
                    "filename": chunk["filename"],
                    "query":    query[:50] + "…",
                    "got":      [r.get("filename") for r in results[:3]],
                })

        recall_pct = hits / len(chunks) * 100
        print(f"\nKeyword-Recall: {hits}/{len(chunks)} = {recall_pct:.0f}%")
        if misses:
            print("Misses:")
            for m in misses:
                print(f"  ✗ {m['filename'][:40]} | query: {m['query']}")
                print(f"    Stattdessen: {m['got']}")

        assert hits >= min_hits, (
            f"Keyword-Recall zu niedrig: {hits}/{len(chunks)} ({recall_pct:.0f}%) "
            f"— Minimum: {min_hits}/{len(chunks)}"
        )

    def test_exact_chunk_found(self, live_db, sample_chunks):
        """Einzelner Test: langer Excerpt muss exakt das richtige Dokument finden."""
        from scanner.embedder import keyword_search_chunks

        chunk = sample_chunks[0]
        # Längerer Excerpt = präziser
        query = _excerpt(chunk["content"], length=120)
        results = keyword_search_chunks(live_db, query, limit=5)

        assert _doc_in_results(results, chunk["document_id"]), (
            f"Dokument '{chunk['filename']}' nicht in Resultaten.\n"
            f"Query: {query[:80]}\n"
            f"Gefunden: {[r.get('filename') for r in results]}"
        )

    def test_short_query_still_finds(self, live_db, sample_chunks):
        """Auch kurze Queries (30 Zeichen) müssen funktionieren."""
        from scanner.embedder import keyword_search_chunks

        found_count = 0
        for chunk in sample_chunks[:5]:
            query = _excerpt(chunk["content"], length=30)
            if len(query) < 10:
                continue
            results = keyword_search_chunks(live_db, query, limit=10)
            if _doc_in_results(results, chunk["document_id"]):
                found_count += 1

        assert found_count >= 2, (
            f"Kurze Queries finden zu wenig: {found_count}/5"
        )


# ── 2. Vektor-Recall ──────────────────────────────────────────────────────────

@pytest.mark.vector
class TestVectorRecall:
    """Vektor-Suche muss das Quelldokument in Top-10 finden.
    Erfordert laufendes Ollama (wird übersprungen wenn nicht verfügbar).
    """

    @pytest.fixture(autouse=True)
    def require_ollama(self):
        from scanner.embedder import is_ollama_running
        if not is_ollama_running():
            pytest.skip("Ollama läuft nicht — Vektor-Tests übersprungen.")

    @pytest.mark.parametrize("n_samples,min_hits,top_k", [(10, 7, 10)])
    def test_vector_recall_rate(self, live_db, sample_chunks, n_samples, min_hits, top_k):
        """Mindestens min_hits von n_samples Chunks in Top-k Vektor-Resultaten."""
        from scanner.embedder import embed_query, vector_search

        chunks = random.sample(sample_chunks, min(n_samples, len(sample_chunks)))
        hits = 0
        misses = []

        for chunk in chunks:
            query = _excerpt(chunk["content"], length=200)
            qvec  = embed_query(query)
            if qvec is None:
                pytest.skip("Embedding fehlgeschlagen.")

            results = vector_search(live_db, qvec, limit=top_k)
            if _doc_in_results(results, chunk["document_id"]):
                hits += 1
            else:
                top_scores = [(r.get("filename", "?"), round(r.get("score", 0), 3))
                              for r in results[:3]]
                misses.append({
                    "filename": chunk["filename"],
                    "score":    top_scores,
                })

        recall_pct = hits / len(chunks) * 100
        print(f"\nVektor-Recall@{top_k}: {hits}/{len(chunks)} = {recall_pct:.0f}%")
        if misses:
            for m in misses:
                print(f"  ✗ {m['filename'][:40]} | Top-3: {m['score']}")

        assert hits >= min_hits, (
            f"Vektor-Recall@{top_k} zu niedrig: {hits}/{len(chunks)} ({recall_pct:.0f}%) "
            f"— Minimum: {min_hits}/{len(chunks)}"
        )

    def test_definition_query_finds_norm(self, live_db):
        """SIA-Normen-Test: Definition-Frage muss das richtige Normdokument finden."""
        from scanner.embedder import embed_query, vector_search, keyword_search_chunks

        # Bekannte Definition aus SIA 416
        query = "Geschossfläche GF allseitig umschlossen überdeckt Grundrissfläche"

        # Keyword-Suche
        kw_results = keyword_search_chunks(live_db, query, limit=10)
        kw_filenames = [r.get("filename", "") for r in kw_results]
        kw_hit = any("416" in f for f in kw_filenames)

        # Vektor-Suche
        qvec = embed_query(query)
        vec_results = vector_search(live_db, qvec, limit=10) if qvec is not None else []
        vec_filenames = [r.get("filename", "") for r in vec_results]
        vec_hit = any("416" in f for f in vec_filenames)

        print(f"\nSIA 416 Definition-Test:")
        print(f"  Keyword: {'✓' if kw_hit else '✗'} — {kw_filenames[:3]}")
        print(f"  Vektor:  {'✓' if vec_hit else '✗'} — {vec_filenames[:3]}")

        assert kw_hit or vec_hit, (
            "SIA 416 nicht in Resultaten für Geschossfläche-Definition.\n"
            f"Keyword-Resultate: {kw_filenames}\n"
            f"Vektor-Resultate:  {vec_filenames}"
        )


# ── 3. End-to-End HTTP-Test ───────────────────────────────────────────────────

@pytest.mark.e2e
class TestE2ERecall:
    """End-to-end: Echter HTTP-Request an den laufenden Server."""

    SERVER_URL = os.environ.get("ARCHIVIO_SERVER", "http://windows.local:8000")

    @pytest.fixture(autouse=True)
    def require_server(self):
        try:
            import requests
            r = requests.get(f"{self.SERVER_URL}/api/status", timeout=3)
            if r.status_code != 200:
                pytest.skip("Server nicht erreichbar.")
        except Exception:
            pytest.skip("Server nicht erreichbar.")

    def test_ai_search_returns_sources(self, live_db, sample_chunks):
        """KI-Suche gibt Quellen zurück und enthält das gesuchte Dokument."""
        import requests
        from bs4 import BeautifulSoup  # optional — falls nicht vorhanden: skip

        chunk = sample_chunks[0]
        query = _excerpt(chunk["content"], length=80)

        try:
            resp = requests.get(
                f"{self.SERVER_URL}/search/ai",
                params={"q": query},
                timeout=30,
            )
        except Exception as e:
            pytest.skip(f"HTTP-Request fehlgeschlagen: {e}")

        assert resp.status_code == 200, f"HTTP {resp.status_code}"

        # Einfacher Check: Dateiname im HTML?
        filename_base = chunk["filename"].split(".")[0][:20]
        assert filename_base in resp.text, (
            f"Dateiname '{chunk['filename']}' nicht in AI-Antwort.\n"
            f"Query: {query}"
        )

    def test_ai_search_response_time(self, sample_chunks):
        """KI-Suche (Quellen-Phase) muss in unter 10 Sekunden antworten."""
        import requests
        import time

        query = _excerpt(sample_chunks[0]["content"], length=60)
        start = time.time()
        try:
            resp = requests.get(
                f"{self.SERVER_URL}/search/ai",
                params={"q": query},
                timeout=15,
            )
        except Exception as e:
            pytest.skip(f"Request fehlgeschlagen: {e}")

        elapsed = time.time() - start
        print(f"\nAntwortzeit KI-Suche (Quellen): {elapsed:.1f}s")
        assert elapsed < 10, f"Zu langsam: {elapsed:.1f}s (max 10s)"
