"""Tests für die produktive Volltextsuche (web/main.py::_run_scoped_search).

Bis Server 3.4.4 prüfte diese Datei die Bewertungslogik von
scanner.embedder.keyword_search_chunks() -- einer zweiten Suchimplementierung, die
ausser der KI-Suche niemand aufrief. Gemessen wurde damit also etwas, das kein Büro je
ausführte. Seit dem Ausbau der KI-Suche gibt es nur noch einen Weg, und diese Tests
gehen ihn: denselben, den /search und /api/mcp/search benutzen.

Die Zerlegung der Suchbegriffe selbst (Bindestrich, kurze Wörter, Wortgrenzen) prüft
tests/test_suchbegriffe.py -- hier geht es um das, was am Ende zurückkommt."""
import pytest

from db import queries


def _dok(conn, project_id, filename, inhalt):
    doc_id = queries.upsert_document(conn, {
        "project_id":   project_id,
        "hash":         f"h-{filename}",
        "filename":     filename,
        "extension":    ".txt",
        "filesize":     len(inhalt),
        "modified_at":  "2026-01-01T00:00:00Z",
        "source_type":  "filesystem",
    })
    queries.set_extraction_status(conn, doc_id, "ok")
    queries.upsert_path(conn, doc_id, f"/scan/{filename}", True)
    queries.upsert_content(conn, doc_id, inhalt)
    queries.save_chunks(conn, doc_id, [{"page_number": None, "chunk_index": 0, "content": inhalt}])
    return doc_id


def _suche(conn, anfrage):
    from web.main import _run_scoped_search
    treffer, fehler = _run_scoped_search(conn, anfrage, "", [], {"docs"})
    assert fehler is None, fehler
    return treffer


def test_wort_im_inhalt_wird_gefunden(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc = _dok(tmp_db, p, "norm.txt", "Die Geschossflaeche wird gemaess SIA 416 berechnet.")
    tmp_db.commit()

    treffer = _suche(tmp_db, "Geschossflaeche")
    assert [t["id"] for t in treffer] == [doc]


def test_wortanfang_genuegt(tmp_db):
    """Wörter ab drei Zeichen suchen als Präfix -- 'geschoss' muss 'Geschossflaeche'
    finden, sonst müsste im Büro jedes Fachwort exakt getippt werden."""
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc = _dok(tmp_db, p, "norm.txt", "Die Geschossflaeche wird gemaess SIA 416 berechnet.")
    tmp_db.commit()

    assert [t["id"] for t in _suche(tmp_db, "geschoss")] == [doc]


def test_mehrere_woerter_muessen_im_selben_dokument_stehen(tmp_db):
    """Grundlage der Null-Treffer-Diagnose: kommen alle Begriffe vor, aber nie
    gemeinsam, darf die Suche nichts liefern -- sonst wäre der Hinweis 'zu eng'
    schlicht falsch."""
    p = queries.insert_project(tmp_db, "P", "/scan")
    beide = _dok(tmp_db, p, "beides.txt", "Treppenhaus und Lift im Hochhaus.")
    _dok(tmp_db, p, "nur_treppe.txt", "Das Treppenhaus wird betoniert.")
    _dok(tmp_db, p, "nur_lift.txt", "Der Lift wird revidiert.")
    tmp_db.commit()

    assert [t["id"] for t in _suche(tmp_db, "treppenhaus lift")] == [beide]
    assert _suche(tmp_db, "treppenhaus zwischendecke") == []


def test_grossschreibung_ist_egal(tmp_db):
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc = _dok(tmp_db, p, "norm.txt", "2 GESCHOSSFLAECHE GF Definition gemaess SIA.")
    tmp_db.commit()

    assert [t["id"] for t in _suche(tmp_db, "geschossflaeche")] == [doc]


def test_treffer_bringt_die_hervorgehobene_fundstelle_mit(tmp_db):
    """Die Trefferliste zeigt den Textausschnitt mit Hervorhebung -- ohne die Marke
    sähe der Nutzer nicht, warum ein Dokument passt."""
    p = queries.insert_project(tmp_db, "P", "/scan")
    _dok(tmp_db, p, "norm.txt", "Die Geschossflaeche wird gemaess SIA 416 berechnet.")
    tmp_db.commit()

    treffer = _suche(tmp_db, "Geschossflaeche")
    assert "<mark>Geschossflaeche</mark>" in treffer[0]["excerpt"]


@pytest.mark.parametrize("anfrage", ["u-wert", "u wert"])
def test_bindestrich_und_leerzeichen_liefern_dasselbe(tmp_db, anfrage):
    """Der Fehler aus v3.4.4: 'u-wert' brach mit 'no such column: wert' ab, 'u wert'
    lieferte über den Präfix 'u*' jedes 'und'. Beide Schreibweisen müssen jetzt
    dasselbe Dokument finden -- und nur dieses."""
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc = _dok(tmp_db, p, "fassade.txt", "Der U-Wert der Fassade betraegt 0.15.")
    _dok(tmp_db, p, "andere.txt", "Eine Bewertung und Beurteilung der Umgebung.")
    tmp_db.commit()

    assert [t["id"] for t in _suche(tmp_db, anfrage)] == [doc]
