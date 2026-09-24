"""Tests für die Migrationen 026, 028 und 029 (Datenbank-Verkleinerung).

026 nahm den Volltext aus `documents_fts` (dort wurde er nie gesucht), 028 den nie
gelesenen Zeitstempel aus `document_chunks`, 029 die Embedding-Spalte -- mit dem Ausbau
der lokalen KI-Suche hat sie keinen Leser mehr. Auf der Produktivdatenbank sind das
652'684 Vektoren zu je 1536 Bytes, rund 1,0 GB.

Die frühere Migration 027 (float32 → float16) hatte hier eigene Tests. Sie sind mit 029
gegenstandslos geworden: in einer frisch migrierten Datenbank existiert die Spalte nicht
mehr, die Tests liessen sich gar nicht mehr aufsetzen. 027 bleibt in der Kette, damit
Datenbanken, die zwischen 3.4.0 und 3.4.4 entstanden sind, dieselbe Reihenfolge sehen.

Das Entscheidende an 029 ist, was NICHT verschwinden darf: `document_chunks.content`,
die FTS-Tabelle `chunks_fts` und ihre Trigger tragen die gesamte Volltextsuche."""
from db import queries


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


# ── 028/029: document_chunks abgespeckt ──────────────────────────────────────

def test_chunks_ohne_created_at_und_ohne_embedding(tmp_db):
    spalten = [r[1] for r in tmp_db.execute("PRAGMA table_info(document_chunks)")]
    assert "created_at" not in spalten
    assert "embedding"  not in spalten
    assert "content"    in spalten


def test_volltextsuche_ueberlebt_den_spaltenabbau(tmp_db):
    """Der Ernstfall: 029 fasst dieselbe Tabelle an, auf der `chunks_fts` sitzt. Fielen
    dabei die Trigger, würde neuer Text stillschweigend nicht mehr indexiert -- die
    Suche wäre kaputt, ohne dass irgendetwas eine Fehlermeldung wirft."""
    pid = queries.insert_project(tmp_db, "P", "/scan")
    doc = queries.upsert_document(tmp_db, {
        "project_id": pid, "hash": "h2", "filename": "statik.pdf", "extension": ".pdf",
        "filesize": 1, "modified_at": None, "source_type": "filesystem"})
    queries.save_chunks(tmp_db, doc, [
        {"page_number": 1, "chunk_index": 0, "content": "Bewehrung nach SIA 262"}])
    tmp_db.commit()

    treffer = tmp_db.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'Bewehrung'").fetchall()
    assert len(treffer) == 1

    tmp_db.execute("DELETE FROM documents WHERE id = ?", (doc,))
    tmp_db.commit()
    assert tmp_db.execute(
        "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'Bewehrung'"
    ).fetchone()[0] == 0


def test_vacuum_marke_wird_nur_bei_echtem_abbau_gesetzt(tmp_db):
    """029 hinterlässt eine Marke, an der die Startvorbereitung ein einmaliges VACUUM
    festmacht -- DROP COLUMN allein gibt den Platz nicht ans Dateisystem zurück. Eine
    frisch angelegte Datenbank hatte die Spalte nie befüllt; dort ist die Marke bereits
    abgearbeitet oder gar nicht nötig, jedenfalls darf sie nicht dauerhaft stehen
    bleiben und bei jedem Start ein VACUUM auslösen."""
    from web.main import _aufraeumen_nach_migration

    tmp_db.execute("INSERT OR IGNORE INTO _migrations (id) VALUES ('029_vacuum_offen')")
    tmp_db.commit()

    _aufraeumen_nach_migration(tmp_db)

    assert tmp_db.execute(
        "SELECT 1 FROM _migrations WHERE id = '029_vacuum_offen'").fetchone() is None
