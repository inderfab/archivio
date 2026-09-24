"""Tests für die Diagnose bei null Treffern (web/main.py::_leertreffer_diagnose).

Anlass ist ein echter Fall: gesucht wurde „260902 Afo Eingabe", die Datei heisst
aber „260209 Afo Eingabe Publikation GBO SG.pdf" — zwei vertauschte Ziffern. Weil
mehrere Wörter mit UND verknüpft werden, gab es null Treffer und keinerlei Hinweis,
dass nur ein einziger Begriff schuld war.

Wichtig: die engere Suche wird NICHT automatisch ausgeführt. Eine Trefferliste für
eine andere als die gestellte Frage wäre in einem Archiv gefährlich — jemand könnte
daraus schliessen, das gesuchte Dokument existiere."""
from fastapi.testclient import TestClient

from db import queries
from web import main as web_main


def _dokument(conn, name: str, inhalt: str, projekt="P", pfad="/scan"):
    pid = conn.execute("SELECT id FROM projects WHERE name=?", (projekt,)).fetchone()
    pid = pid[0] if pid else queries.insert_project(conn, projekt, pfad)
    doc = queries.upsert_document(conn, {
        "project_id": pid, "hash": name, "filename": name, "extension": ".pdf",
        "filesize": 1, "modified_at": None, "source_type": "filesystem"})
    conn.execute("INSERT INTO document_content (document_id, content, language) VALUES (?,?,'de')",
                 (doc, inhalt))
    conn.execute("INSERT INTO document_chunks (document_id, chunk_index, content) VALUES (?,0,?)",
                 (doc, inhalt))
    conn.commit()
    return doc


def _diagnose(conn, frage):
    return web_main._leertreffer_diagnose(conn, frage, "", [],
                                          {"docs", "folders", "filenames"})


def test_nennt_den_begriff_den_es_nicht_gibt(tmp_db):
    """Der reale Fall: eine vertippte Datumszahl."""
    _dokument(tmp_db, "260209 Afo Eingabe Publikation.pdf", "Afo Eingabe Publikation GBO")

    d = _diagnose(tmp_db, "260902 Afo Eingabe")

    assert d is not None
    assert d["fehlend"] == ["260902"]
    assert set(d["rest"]) == {"Afo", "Eingabe"}
    assert d["rest_treffer"] == 1


def test_nennt_den_einengenden_begriff(tmp_db):
    """Kommen alle Begriffe vor, aber nie gemeinsam, wird der Begriff genannt, dessen
    Weglassen Treffer bringt. Realer Fall: »treppenhaus hochhaus lift plan keller
    rietbachstrasse« findet nichts, ohne den Strassennamen aber schon."""
    _dokument(tmp_db, "plan.pdf", "Grundriss Treppenhaus Hochhaus Lift Keller")
    _dokument(tmp_db, "adresse.pdf", "Objekt an der Rietbachstrasse")

    d = _diagnose(tmp_db, "treppenhaus hochhaus lift plan keller rietbachstrasse")

    assert d["art"] == "zu_eng"
    assert d["weglassen"] == ["rietbachstrasse"]
    assert d["rest_treffer"] == 1
    assert "rietbachstrasse" not in d["rest_query"]


def test_zu_lange_anfragen_werden_nicht_zerlegt(tmp_db):
    """Ab sieben Wörtern ist die Anfrage eher ein Satz als eine Suche — dann lohnen
    sich die zusätzlichen Abfragen nicht."""
    _dokument(tmp_db, "plan.pdf", "Grundriss")
    lang = "Grundriss a b c d e f g"
    assert _diagnose(tmp_db, lang) is None or _diagnose(tmp_db, lang)["art"] == "unbekannt"


def test_schweigt_bei_einem_einzigen_wort(tmp_db):
    """Bei einem Wort gibt es nichts zu zerlegen — die Auskunft wäre nur Lärm."""
    _dokument(tmp_db, "plan.pdf", "Grundriss")
    assert _diagnose(tmp_db, "gibtsnicht") is None


def test_findet_woerter_auch_nur_im_dateinamen(tmp_db):
    """Ein Wort, das ausschliesslich im Dateinamen steht, darf nicht als
    unbekannt gemeldet werden."""
    _dokument(tmp_db, "Sonderfall.pdf", "völlig anderer Inhalt")

    d = _diagnose(tmp_db, "Sonderfall gibtsnicht")

    assert d["fehlend"] == ["gibtsnicht"]
    assert d["rest"] == ["Sonderfall"]


def test_mehrere_fehlende_begriffe(tmp_db):
    _dokument(tmp_db, "plan.pdf", "Grundriss Erdgeschoss")
    d = _diagnose(tmp_db, "Grundriss quatsch unfug")
    assert set(d["fehlend"]) == {"quatsch", "unfug"}
    assert d["rest"] == ["Grundriss"]


def test_alle_begriffe_unbekannt(tmp_db):
    _dokument(tmp_db, "plan.pdf", "Grundriss")
    d = _diagnose(tmp_db, "quatsch unfug")
    assert set(d["fehlend"]) == {"quatsch", "unfug"}
    assert d["rest"] == []
    assert d["rest_treffer"] == 0


def test_hinweis_erscheint_in_der_oberflaeche_ohne_automatische_ersatzsuche(tmp_db):
    _dokument(tmp_db, "260209 Afo Eingabe Publikation.pdf", "Afo Eingabe Publikation GBO")

    r = TestClient(web_main.app).get("/search", params={"q": "260902 Afo Eingabe"})

    assert r.status_code == 200
    text = " ".join(r.text.split())          # Zeilenumbrüche aus dem Template glätten
    assert "Keine Dokumente gefunden" in text
    assert "260902" in text and "kommt in keinem Dokument vor" in text
    assert "Ohne diesen Begriff suchen — 1 Treffer" in text
    # Die Ersatztreffer werden angeboten, nicht ausgeliefert
    assert "260209 Afo Eingabe Publikation.pdf" not in text
