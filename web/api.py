"""JSON-API."""
from __future__ import annotations

import os
import subprocess
import threading
import zipfile
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import settings
from db import connection
from web.dashboard import _mail_scan, _run_mail_scan, _run_scan, _scans, _cancel_flags, _now

router = APIRouter(prefix="/api")

_VERSION_FILE = Path(__file__).parent.parent / "VERSION"


@router.get("/version")
async def version():
    v = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "0.0.0"
    return JSONResponse({"version": v})


@router.get("/status")
async def status():
    conn  = connection.get_connection()
    total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    last  = conn.execute("SELECT MAX(indexed_at) FROM documents").fetchone()[0]
    conn.close()

    base_folders = settings.get("scanner.base_folders", [])
    nas_ok  = any(Path(f.get("path", "")).exists() for f in base_folders)
    nas_path = base_folders[0].get("path", "") if base_folders else ""

    return JSONResponse({
        "server":    True,
        "nas":       nas_ok,
        "nas_path":  nas_path,
        "doc_count": total,
        "last_scan": last,
    })


@router.post("/scan/all")
async def scan_all():
    conn     = connection.get_connection()
    projects = conn.execute(
        "SELECT id, path FROM projects WHERE active=1"
    ).fetchall()
    conn.close()

    started = 0
    for p in projects:
        if _scans.get(p["id"], {}).get("status") == "running":
            continue
        _scans[p["id"]] = {"status": "running", "started_at": _now()}
        threading.Thread(
            target=_run_scan, args=(p["id"], p["path"]), daemon=True
        ).start()
        started += 1

    if _mail_scan.get("status") != "running":
        _mail_scan.clear()
        _mail_scan["status"]     = "running"
        _mail_scan["started_at"] = _now()
        threading.Thread(target=_run_mail_scan, daemon=True).start()

    return JSONResponse({"ok": True, "projects_started": started})


@router.get("/scan/nav-status", response_class=None)
async def scan_nav_status():
    """Mini-Indikator für die globale Navigation — leer wenn kein Scan aktiv."""
    from fastapi.responses import HTMLResponse as _HTML
    running = [s for s in _scans.values() if s.get("status") == "running"]
    if not running:
        return _HTML("")
    s         = running[0]
    total     = s.get("total", 0)
    processed = s.get("processed", 0)
    pct       = f"{int(processed/total*100)}%" if total > 0 else "…"
    label     = f"⟳ Scan läuft {pct}" if total > 0 else "⟳ Scan läuft…"
    return _HTML(f'<span class="nav-scan-pill">{label}</span>')


@router.post("/reset-and-rescan")
async def reset_and_rescan():
    """Löscht alle Chunks/Embeddings, setzt alle Dokumente auf 'pending' und startet Scan.
    Der Scanner extrahiert, chunked und embeddet alles neu — korrekt von Anfang an.
    """
    def _reset():
        conn = connection.get_connection()
        try:
            # FTS-Triggers deaktivieren damit DELETE schneller geht
            # (sonst feuert der Trigger für jeden der 24k Chunks einzeln)
            conn.execute("DROP TRIGGER IF EXISTS chunks_fts_delete")
            conn.execute("DROP TRIGGER IF EXISTS chunks_fts_insert")
            conn.execute("DROP TRIGGER IF EXISTS chunks_fts_update")
            # Alle Chunks löschen
            conn.execute("DELETE FROM document_chunks")
            # FTS direkt leeren (kein Trigger mehr)
            try:
                conn.execute("DELETE FROM chunks_fts")
            except Exception:
                pass
            # Alle Dokumente auf pending setzen
            conn.execute("UPDATE documents SET extraction_status = 'pending', content = NULL")
            # Versuche language-Spalte zu leeren falls sie existiert
            try:
                conn.execute("UPDATE documents SET language = NULL")
            except Exception:
                pass
            conn.commit()
            # Triggers neu anlegen
            conn.executescript("""
                CREATE TRIGGER IF NOT EXISTS chunks_fts_insert AFTER INSERT ON document_chunks BEGIN
                    INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS chunks_fts_delete AFTER DELETE ON document_chunks BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES ('delete', old.id, old.content);
                END;
                CREATE TRIGGER IF NOT EXISTS chunks_fts_update AFTER UPDATE ON document_chunks BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES ('delete', old.id, old.content);
                    INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
                END;
            """)
        finally:
            conn.close()

    import asyncio
    await asyncio.get_event_loop().run_in_executor(None, _reset)

    # Scanner für alle aktiven Projekte starten
    conn = connection.get_connection()
    projects = conn.execute("SELECT id, path FROM projects WHERE active=1").fetchall()
    conn.close()
    for p in projects:
        if _scans.get(p["id"], {}).get("status") == "running":
            continue
        _scans[p["id"]] = {"status": "running", "started_at": _now()}
        threading.Thread(target=_run_scan, args=(p["id"], p["path"]), daemon=True).start()

    return JSONResponse({
        "ok": True,
        "message": f"Reset abgeschlossen. {len(projects)} Projekt(e) werden neu gescannt und eingebettet."
    })


@router.post("/fix/garbage-docs")
async def fix_garbage_docs():
    """Findet Dokumente mit unlesbaren Font-Encoding (Mojibake/Zeichensalat) und setzt sie
    auf 'pending' zurück. Danach werden sie beim nächsten Scan mit OCR neu extrahiert.
    """
    def _find_and_reset():
        conn = connection.get_connection()
        try:
            # Alle Chunks laden und auf Mojibake prüfen
            # (Nur Dokumente die als 'ok' markiert sind — 'error'/'pending' ignorieren)
            rows = conn.execute("""
                SELECT dc.document_id, dc.content
                FROM document_chunks dc
                JOIN documents d ON d.id = dc.document_id
                WHERE d.extraction_status = 'ok'
                ORDER BY dc.document_id, dc.chunk_index
            """).fetchall()

            # Pro Dokument: Anteil Replacement-Characters berechnen
            from collections import defaultdict
            doc_chars: dict[int, list] = defaultdict(list)
            for r in rows:
                doc_chars[r["document_id"]].append(r["content"] or "")

            garbage_ids = []
            for doc_id, contents in doc_chars.items():
                sample = "".join(contents[:5])
                if not sample:
                    continue
                ratio = sample.count("�") / len(sample)
                if ratio > 0.30:
                    garbage_ids.append(doc_id)

            if not garbage_ids:
                return 0, []

            # Chunks löschen und Status zurücksetzen
            placeholders = ",".join("?" * len(garbage_ids))
            filenames = [
                r[0] for r in conn.execute(
                    f"SELECT filename FROM documents WHERE id IN ({placeholders})",
                    garbage_ids
                ).fetchall()
            ]
            conn.execute("DROP TRIGGER IF EXISTS chunks_fts_delete")
            conn.execute("DROP TRIGGER IF EXISTS chunks_fts_insert")
            conn.execute("DROP TRIGGER IF EXISTS chunks_fts_update")
            conn.execute(f"DELETE FROM document_chunks WHERE document_id IN ({placeholders})", garbage_ids)
            try:
                conn.execute("DELETE FROM chunks_fts")
            except Exception:
                pass
            conn.execute(
                f"UPDATE documents SET extraction_status='pending', content=NULL WHERE id IN ({placeholders})",
                garbage_ids
            )
            conn.commit()
            # FTS-Triggers neu anlegen
            conn.executescript("""
                CREATE TRIGGER IF NOT EXISTS chunks_fts_insert AFTER INSERT ON document_chunks BEGIN
                    INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS chunks_fts_delete AFTER DELETE ON document_chunks BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES ('delete', old.id, old.content);
                END;
                CREATE TRIGGER IF NOT EXISTS chunks_fts_update AFTER UPDATE ON document_chunks BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES ('delete', old.id, old.content);
                    INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
                END;
            """)
            return len(garbage_ids), filenames
        finally:
            conn.close()

    import asyncio
    count, filenames = await asyncio.get_event_loop().run_in_executor(None, _find_and_reset)

    if count == 0:
        return JSONResponse({"ok": True, "count": 0,
                             "message": "Keine Dokumente mit Zeichensalat gefunden."})

    return JSONResponse({
        "ok": True,
        "count": count,
        "files": filenames,
        "message": f"{count} Dokument(e) zurückgesetzt. Bitte jetzt 'Jetzt scannen' starten — sie werden mit OCR neu extrahiert."
    })


@router.get("/ai/status")
async def ai_status():
    from scanner.embedder import ai_status as _status
    return JSONResponse(_status())


_backfill_state: dict = {"running": False, "done": 0, "total": 0, "error": ""}


@router.post("/ai/backfill")
async def ai_backfill():
    """Berechnet fehlende Embeddings für alle vorhandenen Chunks im Hintergrund."""
    if _backfill_state.get("running"):
        return JSONResponse({"ok": False, "message": "Läuft bereits"})

    def _run():
        from scanner.embedder import is_ollama_running, embed_document_chunks
        _backfill_state.update({"running": True, "done": 0, "total": 0, "error": ""})
        conn = connection.get_connection()
        try:
            if not is_ollama_running():
                _backfill_state["error"] = "Ollama nicht erreichbar"
                return
            doc_ids = conn.execute("""
                SELECT DISTINCT document_id FROM document_chunks
                WHERE embedding IS NULL
            """).fetchall()
            _backfill_state["total"] = len(doc_ids)
            for row in doc_ids:
                embed_document_chunks(conn, row["document_id"])
                _backfill_state["done"] += 1
        except Exception as e:
            _backfill_state["error"] = str(e)
        finally:
            _backfill_state["running"] = False
            conn.close()

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True, "message": "Backfill gestartet"})


@router.get("/ai/backfill/status")
async def ai_backfill_status():
    return JSONResponse(_backfill_state)


_filter_lang_state: dict = {"running": False, "done": 0, "total": 0, "deleted": 0, "error": ""}


@router.post("/ai/filter-german")
async def ai_filter_german():
    """Löscht nicht-deutsche Chunks (FR/IT) und Müll-Chunks (nur Punkte/Zahlen)
    aus Dokumenten die noch fehlende Embeddings haben."""
    if _filter_lang_state.get("running"):
        return JSONResponse({"ok": False, "message": "Läuft bereits"})

    def _is_german(text: str) -> bool:
        """Heuristik: True wenn Text deutsch ist (kein externes Paket nötig)."""
        clean = text.strip()
        if not clean or len(clean) < 30:
            return False
        # Müll-Chunks: überwiegend Punkte/Striche (Inhaltsverzeichnis-Linien)
        meaningful = clean.replace(".", "").replace("-", "").replace(" ", "").replace("\n", "")
        if len(meaningful) < len(clean) * 0.15:
            return False
        t = clean.lower()
        # Deutsche Sonderzeichen
        de_chars = sum(t.count(c) for c in "äöüßÄÖÜ")
        # Französische/Italienische Sonderzeichen
        fr_it_chars = sum(t.count(c) for c in "àâèéêëîïôùûçœæìò")
        # Wenn deutlich mehr FR/IT-Zeichen als DE-Zeichen → nicht deutsch
        if fr_it_chars > de_chars + 3:
            return False
        # Häufige deutsche Wörter
        de_words = {"der", "die", "das", "und", "für", "von", "mit", "bei",
                    "auf", "als", "ist", "sind", "wird", "werden", "nicht",
                    "des", "dem", "den", "einer", "eine", "einem", "auch",
                    "nach", "zum", "zur", "oder", "wenn", "durch", "diese"}
        # Häufige französische Wörter
        fr_words = {"les", "des", "est", "pas", "une", "qui", "que", "sur",
                    "par", "dans", "sont", "avec", "pour", "cette", "ces"}
        # Häufige italienische Wörter
        it_words = {"della", "delle", "degli", "del", "nel", "nella", "negli",
                    "per", "che", "con", "una", "uno", "sono", "essere"}
        words = set(t.split())
        de_score = len(words & de_words)
        fr_score = len(words & fr_words)
        it_score = len(words & it_words)
        # Deutsch wenn mehr DE-Treffer als FR+IT zusammen, oder DE-Sonderzeichen vorhanden
        if de_score == 0 and fr_score == 0 and it_score == 0:
            return True  # Neutral (Zahlen, Fachbegriffe) → behalten
        return de_score >= (fr_score + it_score)

    def _run():
        _filter_lang_state.update({"running": True, "done": 0, "total": 0, "deleted": 0, "error": ""})
        try:
            conn = connection.get_connection()
            conn.execute("PRAGMA busy_timeout = 15000")
            # Nur Chunks aus Dokumenten mit fehlenden Embeddings
            rows = conn.execute("""
                SELECT dc.id, dc.document_id, dc.content
                FROM document_chunks dc
                WHERE dc.embedding IS NULL
                AND dc.content IS NOT NULL
            """).fetchall()
            conn.close()

            _filter_lang_state["total"] = len(rows)
            to_delete = []

            for row in rows:
                if not _is_german(row["content"] or ""):
                    to_delete.append(row["id"])
                _filter_lang_state["done"] += 1

            if to_delete:
                conn = connection.get_connection()
                conn.execute("PRAGMA busy_timeout = 30000")
                batch = 500
                for i in range(0, len(to_delete), batch):
                    ids = to_delete[i:i+batch]
                    ph  = ",".join("?" * len(ids))
                    with conn:
                        conn.execute(f"DELETE FROM document_chunks WHERE id IN ({ph})", ids)
                conn.close()

            _filter_lang_state["deleted"] = len(to_delete)
        except Exception as e:
            _filter_lang_state["error"] = str(e)
        finally:
            _filter_lang_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True, "message": "Sprachfilter gestartet"})


@router.get("/ai/filter-german/status")
async def ai_filter_german_status():
    return JSONResponse(_filter_lang_state)


@router.post("/ai/reset-oversized")
async def ai_reset_oversized():
    """Löscht Chunks von Nicht-PDF Docs die einen einzigen zu-grossen Chunk haben
    und setzt extraction_status auf 'pending' damit der Scanner sie neu verarbeitet."""
    def _run():
        try:
            conn = connection.get_connection()
            conn.execute("PRAGMA busy_timeout = 30000")
            # Alle betroffenen document_ids finden
            rows = conn.execute("""
                SELECT dc.document_id
                FROM document_chunks dc
                JOIN documents d ON d.id = dc.document_id
                WHERE d.extension NOT IN ('.pdf', '')
                AND d.source_type = 'filesystem'
                AND length(dc.content) > 1000
                GROUP BY dc.document_id
                HAVING COUNT(dc.id) = 1
            """).fetchall()
            doc_ids = [r["document_id"] for r in rows]
            if not doc_ids:
                conn.close()
                return 0
            placeholders = ",".join("?" * len(doc_ids))
            with conn:
                conn.execute(f"DELETE FROM document_chunks WHERE document_id IN ({placeholders})", doc_ids)
                conn.execute(f"UPDATE documents SET extraction_status='pending' WHERE id IN ({placeholders})", doc_ids)
            conn.close()
            return len(doc_ids)
        except Exception as e:
            return str(e)

    import asyncio
    result = await asyncio.get_event_loop().run_in_executor(None, _run)
    if isinstance(result, str):
        return JSONResponse({"ok": False, "error": result})
    return JSONResponse({"ok": True, "reset": result,
                         "message": f"{result} Dokumente zurückgesetzt — bitte jetzt Scan starten"})


_rechunk_state: dict = {
    "running": False, "done": 0, "total": 0,
    "fixed": 0, "skipped": 0, "error": "",
    "failed_docs": [],  # [{id, filename, reason}]
}

DOC_TIMEOUT = 60  # Sekunden pro Dokument


@router.post("/ai/rechunk")
async def ai_rechunk():
    """Re-chunked Nicht-PDF-Dokumente die einen einzigen zu-grossen Chunk haben."""
    if _rechunk_state.get("running"):
        return JSONResponse({"ok": False, "message": "Läuft bereits"})

    def _rechunk_one(doc_id: int, parts: list[str]) -> str | None:
        """Führt DELETE+INSERT in eigener Connection aus. Gibt Fehlermeldung zurück oder None."""
        try:
            c = connection.get_connection()
            c.execute("PRAGMA busy_timeout = 5000")
            with c:
                c.execute("DELETE FROM document_chunks WHERE document_id = ?", (doc_id,))
                c.executemany(
                    "INSERT INTO document_chunks (document_id, chunk_index, page_number, content) VALUES (?,?,?,?)",
                    [(doc_id, i, None, part) for i, part in enumerate(parts)]
                )
            c.close()
            return None
        except Exception as e:
            return str(e)

    def _run():
        from scanner.extractors import split_text_into_chunks
        _rechunk_state.update({
            "running": True, "done": 0, "total": 0,
            "fixed": 0, "skipped": 0, "error": "", "failed_docs": [],
        })

        # Kandidaten mit einer einzigen Connection finden
        try:
            conn = connection.get_connection()
            conn.execute("PRAGMA busy_timeout = 30000")
            # Direkte JOIN-Query — funktioniert schnell sobald Migration 005
            # den Index idx_document_chunks_doc erstellt hat
            rows = conn.execute("""
                SELECT d.id AS document_id, d.filename, dc.content
                FROM documents d
                JOIN document_chunks dc ON dc.document_id = d.id
                WHERE d.extraction_status = 'ok'
                AND d.extension NOT IN ('.pdf', '')
                AND d.source_type = 'filesystem'
                AND length(dc.content) > 1000
                GROUP BY d.id
                HAVING COUNT(dc.id) = 1
            """).fetchall()
            conn.close()
        except Exception as e:
            _rechunk_state.update({"running": False, "error": str(e)})
            return

        candidates = [
            {"document_id": r["document_id"], "filename": r["filename"], "content": r["content"]}
            for r in rows
        ]

        _rechunk_state["total"] = len(candidates)

        try:
            for row in candidates:
                doc_id   = row["document_id"]
                filename = row["filename"]
                content  = row.get("content") or ""
                try:
                    parts = split_text_into_chunks(content)
                except Exception as e:
                    _rechunk_state["done"]    += 1
                    _rechunk_state["skipped"] += 1
                    _rechunk_state["failed_docs"].append(
                        {"id": doc_id, "filename": filename, "reason": f"Split-Fehler: {e}"}
                    )
                    continue

                _rechunk_state["done"] += 1

                if len(parts) <= 1:
                    continue

                # Mit Timeout ausführen
                result_holder: list = []
                def _do(doc_id=doc_id, parts=parts, holder=result_holder):
                    holder.append(_rechunk_one(doc_id, parts))

                t = threading.Thread(target=_do, daemon=True)
                t.start()
                t.join(timeout=DOC_TIMEOUT)

                if t.is_alive():
                    log.warning("Rechunk timeout doc %s (%s)", doc_id, filename)
                    _rechunk_state["skipped"] += 1
                    _rechunk_state["failed_docs"].append(
                        {"id": doc_id, "filename": filename, "reason": f"Timeout nach {DOC_TIMEOUT}s"}
                    )
                elif result_holder and result_holder[0] is not None:
                    log.warning("Rechunk doc %s (%s): %s", doc_id, filename, result_holder[0])
                    _rechunk_state["skipped"] += 1
                    _rechunk_state["failed_docs"].append(
                        {"id": doc_id, "filename": filename, "reason": result_holder[0]}
                    )
                else:
                    _rechunk_state["fixed"] += 1
        except Exception as e:
            _rechunk_state["error"] = str(e)
        finally:
            _rechunk_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True, "message": "Re-chunk gestartet"})


@router.get("/ai/rechunk/status")
async def ai_rechunk_status():
    return JSONResponse(_rechunk_state)


@router.get("/ai/embed-test")
async def ai_embed_test():
    """Testet Ollama-Embedding — versucht 3 Chunks aus failing docs, Batch-1 und Batch-20."""
    import asyncio, httpx

    def _run():
        conn = connection.get_connection()
        try:
            # 3 chunks aus Dokumenten die komplett ohne Embedding sind
            rows = conn.execute("""
                SELECT dc.id, dc.content, d.filename, length(dc.content) as clen
                FROM document_chunks dc
                JOIN documents d ON d.id = dc.document_id
                WHERE dc.embedding IS NULL
                ORDER BY clen DESC
                LIMIT 3
            """).fetchall()
            if not rows:
                return {"status": "Alle Chunks haben Embeddings — fertig!"}

            results = []
            for row in rows:
                text = row["content"] or ""
                # Test 1: Einzelner Chunk
                try:
                    resp = httpx.post("http://localhost:11434/api/embed",
                        json={"model": "nomic-embed-text", "input": [text]},
                        timeout=60)
                    data = resp.json()
                    vecs = data.get("embeddings", [])
                    results.append({
                        "filename": row["filename"],
                        "chunk_id": row["id"],
                        "text_len": row["clen"],
                        "http_status": resp.status_code,
                        "ok": len(vecs) > 0 and len(vecs[0]) > 0,
                        "error": data.get("error"),
                    })
                except Exception as e:
                    results.append({
                        "filename": row["filename"],
                        "chunk_id": row["id"],
                        "text_len": row["clen"],
                        "exception": str(e),
                    })
            return {"tests": results}
        finally:
            conn.close()

    result = await asyncio.get_event_loop().run_in_executor(None, _run)
    return JSONResponse(result)


_normalize_state: dict = {"running": False, "done": 0, "total": 0, "changed": 0, "error": ""}


@router.post("/ai/normalize-ligatures")
async def ai_normalize_ligatures():
    """Normalisiert Ligaturen (ﬂ→fl) + OCR-Leerzeichen in Chunk-Inhalten,
    löscht Embeddings ALLER Chunks (vollständige Neuberechnung), baut FTS neu."""
    if _normalize_state.get("running"):
        return JSONResponse({"ok": False, "message": "Läuft bereits"})

    def _run():
        from scanner.extractors import normalize_text
        _normalize_state.update({"running": True, "done": 0, "total": 0, "changed": 0, "error": ""})
        conn = connection.get_connection()
        try:
            rows = conn.execute("SELECT id, content FROM document_chunks").fetchall()
            _normalize_state["total"] = len(rows)
            changed = 0
            for row in rows:
                orig = row["content"] or ""
                norm = normalize_text(orig)
                if norm != orig:
                    conn.execute(
                        "UPDATE document_chunks SET content = ? WHERE id = ?",
                        (norm, row["id"])
                    )
                    changed += 1
                _normalize_state["done"] += 1
            conn.commit()
            _normalize_state["changed"] = changed
            # Alle Embeddings löschen → Backfill muss danach laufen
            conn.execute("UPDATE document_chunks SET embedding = NULL")
            conn.commit()
            # FTS-Index neu aufbauen
            try:
                conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
                conn.commit()
            except Exception as fts_err:
                _normalize_state["error"] = f"FTS-Rebuild: {fts_err}"
        except Exception as e:
            conn.rollback()
            _normalize_state["error"] = str(e)
        finally:
            _normalize_state["running"] = False
            conn.close()

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True, "message": "Normalisierung gestartet — danach Embeddings neu generieren!"})


@router.get("/ai/normalize-ligatures/status")
async def ai_normalize_status():
    return JSONResponse(_normalize_state)


@router.get("/ai/diagnostics")
async def ai_diagnostics():
    """Gibt den Embedding-Zustand als JSON zurück (läuft im Thread-Pool)."""
    import asyncio
    loop = asyncio.get_event_loop()

    def _query():
        conn = connection.get_connection()
        try:
            total_chunks = conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
            with_emb     = conn.execute(
                "SELECT COUNT(*) FROM document_chunks WHERE embedding IS NOT NULL"
            ).fetchone()[0]
            oversized = conn.execute("""
                SELECT COUNT(*) FROM (
                    SELECT dc.document_id
                    FROM document_chunks dc
                    JOIN documents d ON d.id = dc.document_id
                    WHERE d.extension NOT IN ('.pdf')
                    AND length(dc.content) > 1000
                    GROUP BY dc.document_id
                    HAVING COUNT(*) = 1
                )
            """).fetchone()[0]
            # Chunks ohne Embedding nach Grösse aufschlüsseln
            missing_by_size = {}
            for label, lo, hi in [("≤500", 0, 500), ("501-2000", 500, 2000),
                                   ("2001-5000", 2000, 5000), (">5000", 5000, 10**9)]:
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM document_chunks WHERE embedding IS NULL "
                    "AND length(content) > ? AND length(content) <= ?", (lo, hi)
                ).fetchone()[0]
                missing_by_size[label] = cnt
            return {
                "total_chunks":            total_chunks,
                "with_embedding":          with_emb,
                "missing_embedding":       total_chunks - with_emb,
                "missing_by_size":         missing_by_size,
                "oversized_single_chunks": oversized,
                "embedding_coverage_pct":  round(with_emb / total_chunks * 100, 1) if total_chunks else 0,
            }
        finally:
            conn.close()

    data = await loop.run_in_executor(None, _query)
    return JSONResponse(data)


_ollama_install_state: dict = {"running": False, "done": False, "error": "", "log": []}


@router.post("/ai/install-ollama")
async def install_ollama():
    if _ollama_install_state.get("running"):
        return JSONResponse({"ok": False, "message": "Installation läuft bereits"})

    def _run():
        _ollama_install_state.update({"running": True, "done": False, "error": "", "log": []})
        brew = next(
            (p for p in ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"] if Path(p).exists()),
            None
        )
        if not brew:
            _ollama_install_state.update({
                "running": False,
                "error": "Homebrew nicht gefunden. Bitte Homebrew zuerst installieren (brew.sh)."
            })
            return
        try:
            proc = subprocess.Popen(
                [brew, "install", "ollama"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            for line in proc.stdout:
                _ollama_install_state["log"].append(line.rstrip())
            proc.wait()
            if proc.returncode == 0:
                _ollama_install_state.update({"running": False, "done": True})
            else:
                _ollama_install_state.update({
                    "running": False,
                    "error": f"Installation fehlgeschlagen (Exit {proc.returncode})"
                })
        except Exception as e:
            _ollama_install_state.update({"running": False, "error": str(e)})

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True})


@router.get("/ai/install-ollama/status")
async def install_ollama_status():
    return JSONResponse(_ollama_install_state)


_GITHUB_REPO = "inderfab/archivio"


@router.get("/update/check")
async def update_check():
    """Prüft ob eine neue Version auf GitHub verfügbar ist."""
    import requests as _req
    current = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "0.0.0"
    try:
        resp = _req.get(
            f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest",
            timeout=10,
            headers={"Accept": "application/vnd.github+json"},
        )
        if resp.status_code != 200:
            return JSONResponse({"current": current, "update_available": False})
        remote_ver = resp.json().get("tag_name", "").lstrip("v")
        update_available = bool(remote_ver) and remote_ver != current
        return JSONResponse({
            "current": current,
            "latest": remote_ver,
            "update_available": update_available,
            "download_url": "https://inderfab.github.io/archivio/index.html",
        })
    except Exception:
        return JSONResponse({"current": current, "update_available": False})


@router.get("/debug/chunks")
async def debug_chunks(filename: str = "", q: str = ""):
    """Zeigt Chunk-Inhalte für ein Dokument (filename) oder Texte die q enthalten."""
    import asyncio
    loop = asyncio.get_event_loop()
    def _query():
        conn = connection.get_connection()
        try:
            if filename:
                rows = conn.execute("""
                    SELECT dc.id, dc.chunk_index, dc.page_number,
                           length(dc.content) AS len,
                           substr(dc.content, 1, 300) AS preview,
                           dc.embedding IS NOT NULL AS has_emb,
                           d.filename
                    FROM document_chunks dc
                    JOIN documents d ON d.id = dc.document_id
                    WHERE d.filename LIKE ?
                    ORDER BY dc.chunk_index
                    LIMIT 30
                """, (f"%{filename}%",)).fetchall()
            elif q:
                rows = conn.execute("""
                    SELECT dc.id, dc.chunk_index, dc.page_number,
                           length(dc.content) AS len,
                           substr(dc.content, 1, 300) AS preview,
                           dc.embedding IS NOT NULL AS has_emb,
                           d.filename
                    FROM document_chunks dc
                    JOIN documents d ON d.id = dc.document_id
                    WHERE LOWER(dc.content) LIKE ?
                    LIMIT 20
                """, (f"%{q.lower()}%",)).fetchall()
            else:
                return {"error": "filename oder q Parameter erforderlich"}
            return {"count": len(rows), "chunks": [dict(r) for r in rows]}
        finally:
            conn.close()
    data = await loop.run_in_executor(None, _query)
    return JSONResponse(data)

@router.get("/debug/dist")
async def debug_dist():
    from web.dashboard import _DIST
    files = list(_DIST.glob("*.zip")) if _DIST.exists() else []
    return {"dist_path": str(_DIST), "exists": _DIST.exists(), "files": [f.name for f in files]}


# ── Recall-Tests ──────────────────────────────────────────────────────────────

@router.get("/test/recall")
async def test_recall(n: int = 10, seed: int = 42):
    """
    Recall-Test: Zufällige Chunks aus der DB → Suche mit Textausschnitt → Dokument gefunden?
    Läuft direkt auf dem Server wo die DB liegt.
    ?n=10   Anzahl Chunks (default 10)
    ?seed=  Zufalls-Seed für Reproduzierbarkeit
    """
    import asyncio, random, time
    loop = asyncio.get_event_loop()

    def _run():
        import random as _rnd
        from scanner.embedder import keyword_search_chunks, embed_query, vector_search, is_ollama_running

        _rnd.seed(seed)
        conn = connection.get_connection()
        results = {"keyword": [], "vector": [], "summary": {}}

        try:
            # Mehr Kandidaten holen, dann Python-seitig filtern
            candidates = conn.execute("""
                SELECT dc.id, dc.document_id, dc.content, dc.chunk_index,
                       dc.page_number, d.filename, d.extension
                FROM document_chunks dc
                JOIN documents d ON d.id = dc.document_id
                WHERE dc.embedding IS NOT NULL
                  AND length(dc.content) >= 200
                  AND d.extraction_status = 'ok'
                  AND d.extension IN ('.pdf', '.docx', '.txt', '.doc')
                ORDER BY RANDOM()
                LIMIT ?
            """, (n * 5,)).fetchall()

            # Mojibake und OCR-Müll filtern: mind. 55% echte Buchstaben im Content
            import re as _re2
            def _is_clean(row):
                text = row["content"] or ""
                if not text:
                    return False
                alpha = sum(c.isalpha() for c in text)
                if alpha / len(text) < 0.55:
                    return False
                # Keine mehrheitlich französischen/italienischen Zeilen
                fr_it_hits = len(_re2.findall(
                    r'\b(le|la|les|de|du|des|une|sur|est|pour|'
                    r'della|delle|degli|con|per|nel|sul|che)\b',
                    text.lower()))
                if fr_it_hits > 5:
                    return False
                return True

            rows = [r for r in candidates if _is_clean(r)][:n]
            if not rows:
                return {"error": "Keine geeigneten Chunks in der DB."}

            def _excerpt(text, length=100):
                """Wählt den besten Satz aus dem Text: deutsch, keine Zahlen-Zeilen."""
                import re as _re
                lines = [l.strip() for l in text.split('\n') if len(l.strip()) >= 40]
                # Qualitätskriterien: mehrheitlich Buchstaben, kein reines OCR-Rauschen
                def _quality(line):
                    alpha = sum(c.isalpha() for c in line)
                    if alpha / max(len(line), 1) < 0.5:   # < 50% Buchstaben → Zahlen/Symbole
                        return 0
                    # Französisch/Italienisch erkennen (häufige Funktionswörter)
                    fr_it = len(_re.findall(r'\b(le|la|les|de|du|des|au|aux|une|sur|est|pour|'
                                           r'il|che|della|delle|degli|con|per|nel|sul)\b',
                                           line.lower()))
                    if fr_it >= 2:                         # mehrere FR/IT Wörter → überspringen
                        return 0
                    return alpha  # höher = besser
                scored = [(l, _quality(l)) for l in lines]
                good = [(l, s) for l, s in scored if s > 0]
                if not good:
                    # Fallback: einfach Mitte nehmen
                    text = text.strip()
                    mid = len(text) // 2
                    return text[max(0, mid - length // 2):mid + length // 2].strip()
                # Besten Satz aus der Mitte des Textes bevorzugen
                mid_idx = len(good) // 2
                best_line = good[mid_idx][0]
                return best_line[:length].strip()

            # ── Keyword-Recall ────────────────────────────────────────────────
            kw_hits = 0
            for row in rows:
                query   = _excerpt(row["content"], length=70)
                hits    = keyword_search_chunks(conn, query, limit=10)
                found   = any(h["document_id"] == row["document_id"] for h in hits)
                kw_hits += int(found)
                results["keyword"].append({
                    "filename":  row["filename"],
                    "page":      row["page_number"],
                    "query":     query[:60] + ("…" if len(query) > 60 else ""),
                    "found":     found,
                    "top3":      [h["filename"] for h in hits[:3]],
                })

            # ── Vektor-Recall ─────────────────────────────────────────────────
            vec_hits      = 0
            ollama_running = is_ollama_running()
            if ollama_running:
                for row in rows:
                    query  = _excerpt(row["content"], length=200)
                    t0     = time.time()
                    qvec   = embed_query(query)
                    hits   = vector_search(conn, qvec, limit=10) if qvec is not None else []
                    found  = any(h["document_id"] == row["document_id"] for h in hits)
                    vec_hits += int(found)
                    results["vector"].append({
                        "filename":    row["filename"],
                        "page":        row["page_number"],
                        "found":       found,
                        "embed_ms":    round((time.time() - t0) * 1000),
                        "top3":        [h["filename"] for h in hits[:3]],
                        "top3_scores": [round(h.get("score", 0), 3) for h in hits[:3]],
                    })

            # ── Summary ───────────────────────────────────────────────────────
            total = len(rows)
            results["summary"] = {
                "total_chunks_tested": total,
                "keyword_hits":        kw_hits,
                "keyword_recall_pct":  round(kw_hits / total * 100, 1),
                "keyword_pass":        kw_hits >= total * 0.7,
                "vector_tested":       ollama_running,
                "vector_hits":         vec_hits if ollama_running else None,
                "vector_recall_pct":   round(vec_hits / total * 100, 1) if ollama_running else None,
                "vector_pass":         (vec_hits >= total * 0.7) if ollama_running else None,
                "overall_pass":        kw_hits >= total * 0.7 and (not ollama_running or vec_hits >= total * 0.7),
            }

        finally:
            conn.close()

        return results

    data = await loop.run_in_executor(None, _run)
    return JSONResponse(data)


@router.get("/test/recall/multi")
async def test_recall_multi(n: int = 10, runs: int = 5):
    """Mehrere Recall-Durchläufe mit verschiedenen Seeds → statistisch belastbares Ergebnis."""
    import asyncio
    loop = asyncio.get_event_loop()

    async def _one_run(seed):
        return await loop.run_in_executor(None, lambda: None)  # placeholder

    # Sequenziell laufen lassen (DB-Last verteilen)
    import httpx
    base = "http://localhost:8000"
    results = []
    for seed in range(runs):
        try:
            resp = httpx.get(f"{base}/api/test/recall?n={n}&seed={seed*17+42}", timeout=300)
            if resp.status_code == 200:
                results.append(resp.json()["summary"])
        except Exception:
            pass

    if not results:
        return {"error": "Keine Ergebnisse"}

    avg_kw  = round(sum(r["keyword_recall_pct"] for r in results) / len(results), 1)
    avg_vec = round(sum(r["vector_recall_pct"] for r in results if r.get("vector_recall_pct") is not None) / len(results), 1)
    hybrids = []
    for r in results:
        kw = r.get("keyword_hits", 0)
        vec = r.get("vector_hits", 0) or 0
        total = r.get("total_chunks_tested", 1)
        # Hybrid nicht direkt verfügbar, approximieren
        hybrids.append(round(min(kw + vec * 0.5, total) / total * 100, 1))

    return {
        "runs": len(results),
        "n_per_run": n,
        "avg_keyword_recall_pct": avg_kw,
        "avg_vector_recall_pct": avg_vec,
        "keyword_pass": avg_kw >= 70,
        "vector_pass": avg_vec >= 70,
        "per_run": results,
    }
