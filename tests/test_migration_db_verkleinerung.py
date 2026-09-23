"""Tests für die Migrationen 026–028 (Datenbank-Verkleinerung).

Gemessen an der Entwicklungsdatenbank: 272,9 MB → 183,6 MB (−32,7 %).

Der heikle Teil ist Migration 027: der Datentyp der Embeddings steht nirgends in der
Datenbank. Wird ein bereits umgewandelter float16-Blob versehentlich ein zweites Mal
als float32 gelesen, entsteht unwiederbringlicher Unsinn -- und weil embedder.py aus
allen Zeilen EINE Matrix baut, fällt danach die gesamte semantische Suche aus.
Deshalb hier vor allem die Wiederaufnahme- und Wiederholungsfälle."""
import numpy as np

from db import migrations, queries


def _chunk_mit_embedding(conn, dtype, dims=8, wert=0.5):
    pid = queries.insert_project(conn, f"P{wert}", f"/scan/{wert}")
    doc = queries.upsert_document(conn, {
        "project_id": pid, "hash": f"h{wert}", "filename": "a.pdf", "extension": ".pdf",
        "filesize": 1, "modified_at": None, "source_type": "filesystem"})
    vec = (np.ones(dims, dtype=np.float32) * wert)
    vec = (vec / np.linalg.norm(vec)).astype(dtype)
    conn.execute(
        "INSERT INTO document_chunks (document_id, chunk_index, content, embedding) "
        "VALUES (?, 0, 'text', ?)", (doc, vec.tobytes()))
    conn.commit()
    return conn.execute("SELECT id FROM document_chunks ORDER BY id DESC LIMIT 1").fetchone()[0]


def _migration_zuruecksetzen(conn, *ids):
    for mid in ids:
        conn.execute("DELETE FROM _migrations WHERE id = ? OR id LIKE ?", (mid, mid + "%"))
    conn.commit()


# ── 026: documents_fts ohne Volltext ─────────────────────────────────────────

def test_documents_fts_hat_nur_noch_dateiname(tmp_db):
    spalten = [r[1] for r in tmp_db.execute("PRAGMA table_info(documents_fts)")]
    assert spalten == ["filename"]


def test_dateiname_wird_weiter_indexiert_und_geloescht(tmp_db):
    pid = queries.insert_project(tmp_db, "P", "/scan")
    doc = queries.upsert_document(tmp_db, {
        "project_id": pid, "hash": "h1", "filename": "grundriss.pdf", "extension": ".pdf",
        "filesize": 1, "modified_at": None, "source_type": "filesystem"})
    tmp_db.commit()

    assert tmp_db.execute(
        "SELECT rowid FROM documents_fts WHERE documents_fts MATCH 'grundriss'"
    ).fetchone()[0] == doc

    tmp_db.execute("DELETE FROM documents WHERE id = ?", (doc,))
    tmp_db.commit()
    assert tmp_db.execute(
        "SELECT 1 FROM documents_fts WHERE rowid = ?", (doc,)).fetchone() is None


# ── 027: Embeddings float16 ──────────────────────────────────────────────────

def test_wandelt_float32_nach_float16(tmp_db):
    cid = _chunk_mit_embedding(tmp_db, np.float32, wert=0.5)
    assert len(tmp_db.execute(
        "SELECT embedding FROM document_chunks WHERE id=?", (cid,)).fetchone()[0]) == 32

    _migration_zuruecksetzen(tmp_db, "027_embeddings_float16", "027_quelllaenge_")
    migrations.run(tmp_db)

    blob = tmp_db.execute("SELECT embedding FROM document_chunks WHERE id=?", (cid,)).fetchone()[0]
    assert len(blob) == 16
    vec = np.frombuffer(blob, dtype=np.float16).astype(np.float32)
    assert np.allclose(vec, np.ones(8) / np.sqrt(8), atol=1e-3)


def test_zweiter_lauf_zerstoert_die_vektoren_nicht(tmp_db):
    """Der gefährlichste Fall: die Migration läuft komplett durch, bricht aber vor dem
    Eintrag in _migrations ab. Beim Neustart sehen alle Zeilen gleich aus -- ohne
    Merker würde ein zweiter Lauf sie für float32 halten und zerschiessen."""
    cid = _chunk_mit_embedding(tmp_db, np.float32, wert=0.5)
    _migration_zuruecksetzen(tmp_db, "027_embeddings_float16", "027_quelllaenge_")
    migrations.run(tmp_db)
    nach_erstem = tmp_db.execute(
        "SELECT embedding FROM document_chunks WHERE id=?", (cid,)).fetchone()[0]

    # Nur den Abschluss-Eintrag entfernen, den Längen-Merker bewusst stehen lassen --
    # genau der Zustand nach einem Absturz zwischen Umwandlung und Eintrag.
    tmp_db.execute("DELETE FROM _migrations WHERE id = '027_embeddings_float16'")
    tmp_db.commit()
    migrations.run(tmp_db)

    assert tmp_db.execute(
        "SELECT embedding FROM document_chunks WHERE id=?", (cid,)).fetchone()[0] == nach_erstem


def test_teilweise_umgewandelte_datenbank_wird_fertig_migriert(tmp_db):
    """Abbruch mittendrin: beide Formate liegen nebeneinander."""
    alt = _chunk_mit_embedding(tmp_db, np.float32, wert=0.5)
    neu = _chunk_mit_embedding(tmp_db, np.float16, wert=0.25)
    _migration_zuruecksetzen(tmp_db, "027_embeddings_float16", "027_quelllaenge_")

    migrations.run(tmp_db)

    laengen = {r[0] for r in tmp_db.execute(
        "SELECT DISTINCT length(embedding) FROM document_chunks WHERE embedding IS NOT NULL")}
    assert laengen == {16}
    # Der bereits umgewandelte Vektor muss unverändert geblieben sein
    vec = np.frombuffer(tmp_db.execute(
        "SELECT embedding FROM document_chunks WHERE id=?", (neu,)).fetchone()[0],
        dtype=np.float16).astype(np.float32)
    assert np.allclose(vec, np.ones(8) / np.sqrt(8), atol=1e-3)
    assert alt is not None


def test_leere_datenbank_ist_kein_fehler(tmp_db):
    _migration_zuruecksetzen(tmp_db, "027_embeddings_float16", "027_quelllaenge_")
    migrations.run(tmp_db)   # darf nicht werfen
    assert tmp_db.execute(
        "SELECT 1 FROM _migrations WHERE id='027_embeddings_float16'").fetchone() is not None


# ── 028: created_at ──────────────────────────────────────────────────────────

def test_chunks_ohne_created_at(tmp_db):
    spalten = [r[1] for r in tmp_db.execute("PRAGMA table_info(document_chunks)")]
    assert "created_at" not in spalten
    assert "embedding" in spalten and "content" in spalten
