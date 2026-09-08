"""Tests für scanner/norms.py — Klassifizierer + MCP-Gate, gemäss Abnahmekriterien
der Spec (archivio-normerkennung-spec.md §8)."""
import pytest
from scanner.norms import (
    NormClassifier, is_norm_doc, redact_hits, guard_read, assert_no_norm_text,
    NORM_NOTICE, load_config, _is_under,
)
from db import queries
import scanner.norms as norms_mod


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def classifier(cfg):
    return NormClassifier(cfg)


# ── §8.1/2: Portabilität — Inhaltssignal ohne jede Pfad-/Ordnerkonfiguration ────

def test_norm_recognized_by_content_alone(classifier):
    """'102.pdf': Dateiname trägt kein Signal, Pfad ist unbekannt. Erkannt wird er
    über Herausgebervermerk + Normnummer im Kopfbereich -- portabel."""
    text = (
        "Schweizerische Norm\nSN 505 102\nNorm für die Berechnung von Bauwerken\n"
        "Lizenziert für Musterbüro AG"
    )
    v = classifier.classify("/beliebig/anders/benannt/102.pdf", text)
    assert v.is_norm is True
    assert "publisher" in v.reason


def test_norm_recognized_regardless_of_folder_name(classifier):
    """Gleiche Datei in beliebig anders benanntem Ordner -> gleiches Ergebnis."""
    text = "Schweizerischer Ingenieur- und Architektenverein\nSIA 118\nCopyright © SIA"
    for folder in ("06_Vorschriften", "Normen SIA", "irgendwas/tief/verschachtelt"):
        v = classifier.classify(f"/{folder}/vertrag.pdf", text)
        assert v.is_norm is True, folder


# ── §8.4: falsche Normnummer im Dateinamen ändert nichts ───────────────────────

def test_wrong_norm_number_in_filename_still_detected_via_content(classifier):
    """'SN 40 291 Parkieren.pdf' -- Dateiname traegt die FALSCHE Normnummer (echte
    Norm ist SN 640 291a), trotzdem is_norm=1 dank Inhalt."""
    text = "Schweizerische Norm\nSN 640 291a\nParkierung\nLizenz-Nr 12345"
    v = classifier.classify("/Normen/SN 40 291 Parkieren.pdf", text)
    assert v.is_norm is True


# ── §8.10-12: Falsch-Positive — notwendige Bedingung ────────────────────────────

def test_law_without_publisher_marker_not_flagged(classifier):
    """Gemeinfreies Gesetz/Verordnung (URG Art. 5) traegt keinen SIA-/VSS-
    Herausgebervermerk -- darf NIE gesperrt werden, auch bei Normnummer-Erwaehnung."""
    text = "Kanton Zürich, Verordnung über Bauvorschriften, gemäss SIA 118 Art. 87 anzuwenden."
    v = classifier.classify("/Gesetze/Bauverordnung.pdf", text)
    assert v.is_norm is False
    assert v.reason is None


def test_note_citing_norm_number_repeatedly_not_flagged(classifier):
    """Aktennotiz mit 'SIA 118' im Dateinamen und mehrfacher Normnennung im Text,
    aber ohne Herausgeber-/Lizenzmarker -- bleibt lesbar (Zitatrecht)."""
    text = "Besprechung zu SIA 118: Art. 87 SIA 118 wurde diskutiert. SIA 118 gilt weiter."
    v = classifier.classify("/Projekt/SIA 118 Diskussion.pdf", text)
    assert v.is_norm is False


def test_filled_contract_with_publisher_only_not_flagged(classifier):
    """Ausgefüllter SIA-Werkvertrag: Herausgeber allein (2 Punkte) reicht nicht für
    Schwelle 4."""
    text = "Schweizerischer Ingenieur- und Architektenverein\nWerkvertrag Projekt Keller"
    v = classifier.classify("/Projekt/Werkvertrag.pdf", text)
    assert v.is_norm is False


# ── §8.16: Umlaut-Pfad NFC/NFD ──────────────────────────────────────────────────

def test_umlaut_path_matches_nfc_and_nfd():
    nfc = "/Büro/Behörden/Normen"
    nfd = unicodedata_normalize_nfd(nfc)
    assert _is_under(nfc + "/datei.pdf", nfd)
    assert _is_under(nfd + "/datei.pdf", nfc)


def unicodedata_normalize_nfd(s: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFD", s)


def test_is_under_does_not_match_sibling_prefix():
    """'/x/Normen2' darf NICHT gegen '/x/Normen' matchen (reiner String-Prefix
    ohne Pfadkomponenten-Grenze wuerde das faelschlich tun)."""
    assert _is_under("/x/Normen/a.pdf", "/x/Normen") is True
    assert _is_under("/x/Normen2/a.pdf", "/x/Normen") is False


# ── Layer 2: gelernter/bestätigter Ordner ───────────────────────────────────────

def test_confirmed_folder_overrides_missing_text_layer(classifier):
    """Ein Scan ohne Textlayer liefert kein Inhaltssignal -- Auffangnetz ist der
    bestätigte Ordner."""
    c = NormClassifier(classifier.cfg, confirmed_folders=["/Normen/SIA"])
    v = c.classify("/Normen/SIA/scan_ohne_text.pdf", text=None)
    assert v.is_norm is True
    assert v.reason == "folder:confirmed"


def test_folder_is_norm_standalone():
    cfg = load_config()
    c = NormClassifier(cfg, confirmed_folders=["/Normen"])
    assert c.folder_is_norm("/Normen/x.pdf") is True
    assert c.folder_is_norm("/Andere/x.pdf") is False


# ── §5: MCP-Gate — is_norm_doc / redact_hits / guard_read ──────────────────────

def _make_doc(conn, project_id, filename, is_norm=0, path=None):
    doc_id = queries.upsert_document(conn, {
        "project_id":  project_id,
        "hash":        f"h-{filename}",
        "filename":    filename,
        "extension":   ".pdf",
        "filesize":    10,
        "modified_at": "2026-01-01T00:00:00Z",
        "source_type": "filesystem",
    })
    queries.set_extraction_status(conn, doc_id, "ok")
    queries.upsert_path(conn, doc_id, path or f"/scan/{filename}", True)
    if is_norm:
        conn.execute("UPDATE documents SET is_norm = 1 WHERE id = ?", (doc_id,))
    conn.commit()
    return doc_id


def test_is_norm_doc_reads_db_flag(tmp_db):
    norms_mod._classifier = None  # frischer Singleton pro Test
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = _make_doc(tmp_db, p, "sia118.pdf", is_norm=1)
    assert is_norm_doc(tmp_db, doc_id, None) is True


def test_is_norm_doc_false_for_normal_document(tmp_db):
    norms_mod._classifier = None
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = _make_doc(tmp_db, p, "bericht.pdf", is_norm=0)
    assert is_norm_doc(tmp_db, doc_id, "/scan/bericht.pdf") is False


def test_is_norm_doc_fail_closed_on_db_error(tmp_db):
    """Simulierter DB-Fehler beim Lookup -> gesperrt, nicht offen (§8.15)."""
    norms_mod._classifier = None

    class _BrokenConn:
        def execute(self, *a, **kw):
            raise RuntimeError("DB kaputt")

    assert is_norm_doc(_BrokenConn(), 1, "/x/y.pdf") is True


def test_redact_hits_replaces_excerpt_and_content(tmp_db):
    norms_mod._classifier = None
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = _make_doc(tmp_db, p, "sia118.pdf", is_norm=1)
    hits = [{"id": doc_id, "path": "/scan/sia118.pdf", "excerpt": "geheimer Normtext"}]
    redacted = redact_hits(tmp_db, hits)
    assert redacted[0]["excerpt"] == NORM_NOTICE
    assert redacted[0]["is_norm"] is True


def test_redact_hits_leaves_normal_documents_untouched(tmp_db):
    norms_mod._classifier = None
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = _make_doc(tmp_db, p, "bericht.pdf", is_norm=0)
    hits = [{"id": doc_id, "path": "/scan/bericht.pdf", "excerpt": "normaler Auszug"}]
    redacted = redact_hits(tmp_db, hits)
    assert redacted[0]["excerpt"] == "normaler Auszug"
    assert "is_norm" not in redacted[0]


def test_guard_read_denies_norm_with_actionable_hint(tmp_db):
    norms_mod._classifier = None
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = _make_doc(tmp_db, p, "sia118.pdf", is_norm=1)
    msg = guard_read(tmp_db, doc_id, "sia118.pdf", "/scan/sia118.pdf")
    assert msg is not None
    assert "als Norm klassifiziert" in msg
    assert f"open_file({doc_id})" in msg
    assert f"reveal_file({doc_id})" in msg


def test_guard_read_allows_normal_document(tmp_db):
    norms_mod._classifier = None
    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = _make_doc(tmp_db, p, "bericht.pdf", is_norm=0)
    assert guard_read(tmp_db, doc_id, "bericht.pdf", "/scan/bericht.pdf") is None


def test_assert_no_norm_text_raises_on_leak():
    with pytest.raises(RuntimeError, match="Norm-Text-Leck"):
        assert_no_norm_text([{"id": 1, "is_norm": True, "excerpt": "geheimer Text"}])


def test_assert_no_norm_text_passes_when_redacted():
    assert_no_norm_text([{"id": 1, "is_norm": True, "excerpt": NORM_NOTICE}])


def test_assert_no_norm_text_ignores_non_norm_hits():
    assert_no_norm_text([{"id": 1, "is_norm": False, "excerpt": "alles ok"}])


def test_looks_like_norm_query_detects_sia_number(tmp_db):
    from scanner.norms import looks_like_norm_query
    assert looks_like_norm_query(tmp_db, "versuche die SIA 400 zu lesen") == "SIA 400"


def test_looks_like_norm_query_detects_en_iso(tmp_db):
    from scanner.norms import looks_like_norm_query
    assert looks_like_norm_query(tmp_db, "EN 1090 Anforderungen") is not None


def test_looks_like_norm_query_none_for_normal_query(tmp_db):
    from scanner.norms import looks_like_norm_query
    assert looks_like_norm_query(tmp_db, "Grundriss Erdgeschoss") is None


def test_looks_like_norm_query_none_for_empty_query(tmp_db):
    from scanner.norms import looks_like_norm_query
    assert looks_like_norm_query(tmp_db, "") is None


def test_find_norm_locations_finds_by_filename(tmp_db):
    from scanner.norms import find_norm_locations

    p = queries.insert_project(tmp_db, "Nicht freigegeben", "/scan")
    _make_doc(tmp_db, p, "SIA 400 Plandarstellung.pdf", is_norm=1,
              path="/scan/Normen/SIA 400 Plandarstellung.pdf")
    tmp_db.commit()

    locations = find_norm_locations(tmp_db, "SIA 400")
    assert len(locations) == 1
    assert locations[0]["filename"] == "SIA 400 Plandarstellung.pdf"
    assert locations[0]["project"] == "Nicht freigegeben"
    assert "SIA 400" in locations[0]["path"]


def test_find_norm_locations_ignores_non_norm_documents(tmp_db):
    from scanner.norms import find_norm_locations

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc(tmp_db, p, "SIA 400 Referenz in Protokoll.pdf", is_norm=0)
    tmp_db.commit()

    assert find_norm_locations(tmp_db, "SIA 400") == []


def test_find_norm_locations_empty_for_unknown_norm(tmp_db):
    from scanner.norms import find_norm_locations

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc(tmp_db, p, "SIA 118.pdf", is_norm=1)
    tmp_db.commit()

    assert find_norm_locations(tmp_db, "SIA 400") == []


def test_find_norm_locations_finds_bare_filename_without_publisher_in_name(tmp_db):
    """Reale Normdateien heissen oft schlicht "400.pdf" -- "SIA" steht nur im
    Ordnernamen, nicht direkt neben der Zahl im Dateinamen. Ein früherer Abgleich
    verlangte "sia" und "400" direkt nebeneinander im GANZEN Pfad und fand dadurch
    selbst offiziell abgelegte Normen nicht (siehe Docstring von find_norm_locations)."""
    from scanner.norms import find_norm_locations

    p = queries.insert_project(tmp_db, "Normen", "/scan")
    _make_doc(tmp_db, p, "400.pdf", is_norm=1,
              path="/scan/Normen_SIA/S I A NORMEN/Normenwerk/400.pdf")
    tmp_db.commit()

    locations = find_norm_locations(tmp_db, "SIA 400")
    assert len(locations) == 1
    assert locations[0]["filename"] == "400.pdf"


def test_find_norm_locations_does_not_match_longer_number(tmp_db):
    """"400" darf nicht als Teilstring einer anderen Zahl wie "4009" oder "1400"
    treffen -- Ziffern-Grenze, nicht blosser Teilstring-Vergleich."""
    from scanner.norms import find_norm_locations

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc(tmp_db, p, "4009.pdf", is_norm=1)
    tmp_db.commit()

    assert find_norm_locations(tmp_db, "SIA 400") == []


def test_find_norm_locations_returns_all_copies(tmp_db):
    """Existieren mehrere Kopien derselben Norm (z.B. aktuelle Fassung + alte
    Fassung in einem Archiv-Unterordner, oder eine Kopie in fremdem Kontext wie
    Schulungsunterlagen), sollen ALLE gefunden werden -- nicht nur die erste."""
    from scanner.norms import find_norm_locations

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc(tmp_db, p, "400.pdf", is_norm=1, path="/scan/Normenwerk/400.pdf")
    _make_doc(tmp_db, p, "400_d.pdf", is_norm=1, path="/scan/z_Normen Alt/400_d.pdf")
    _make_doc(tmp_db, p, "400_Schnupperlehre_Anleitung.pdf", is_norm=1,
              path="/scan/Schnupperlehre/400_Schnupperlehre_Anleitung.pdf")
    tmp_db.commit()

    locations = find_norm_locations(tmp_db, "SIA 400")
    assert {l["filename"] for l in locations} == {
        "400.pdf", "400_d.pdf", "400_Schnupperlehre_Anleitung.pdf",
    }


def test_find_norm_locations_includes_validity_fields(tmp_db):
    from scanner.norms import find_norm_locations

    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = _make_doc(tmp_db, p, "400.pdf", is_norm=1, path="/scan/400.pdf")
    tmp_db.execute(
        "UPDATE documents SET norm_valid_from=?, norm_check_status=? WHERE id=?",
        ("2018-04-01", "aktuell", doc_id),
    )
    tmp_db.commit()

    locations = find_norm_locations(tmp_db, "SIA 400")
    assert locations[0]["valid_from"] == "2018-04-01"
    assert locations[0]["check_status"] == "aktuell"


# ── extract_valid_from(): echte Textmuster aus realen Norm-Titelseiten ─────────

def test_extract_valid_from_finds_explicit_date():
    from scanner.norms import extract_valid_from

    text = "SIA 180.081 Bauwesen\nHerausgeber\nGültig ab: 2018-04-01\nSchweizerischer..."
    assert extract_valid_from(text) == "2018-04-01"


def test_extract_valid_from_falls_back_to_designation_year():
    from scanner.norms import extract_valid_from

    text = "Schweizer Norm\nSIA 400:2000 Bauwesen\nErsetzt Empfehlung SIA 400, Ausgabe 1985"
    assert extract_valid_from(text) == "2000-01-01"


def test_extract_valid_from_falls_back_to_copyright_year():
    from scanner.norms import extract_valid_from

    text = "Empfehlung SIA 113\nFM-gerechte Bauplanung\nCopyright © 2010 by SIA Zurich"
    assert extract_valid_from(text) == "2010-01-01"


def test_extract_valid_from_copyright_tolerates_ocr_misreads():
    """OCR liest "Copyright ©" auf gescannten Normen oft als "Copvright @" ein
    (SIA-Normen mit Texterkennung OCR) -- das Muster muss das trotzdem finden."""
    from scanner.norms import extract_valid_from

    text = "Merkblatt SIA 2017\nCopvright @ 2000 by SIA Zürich"
    assert extract_valid_from(text) == "2000-01-01"


def test_extract_valid_from_finds_inkrafttreten_date():
    from scanner.norms import extract_valid_from

    text = "Es tritt am 1. Juli 2000 in Kraft."
    assert extract_valid_from(text) == "2000-07-01"


def test_extract_valid_from_inkrafttreten_pads_single_digit_day():
    from scanner.norms import extract_valid_from

    text = "Es tritt am 5. März 2015 in Kraft."
    assert extract_valid_from(text) == "2015-03-05"


def test_extract_valid_from_inkrafttreten_beats_copyright_fallback():
    """Wenn beides im Text steht, ist das exakte Inkrafttreten-Datum genauer als
    das blosse Copyright-Jahr und muss gewinnen."""
    from scanner.norms import extract_valid_from

    text = "Es tritt am 1. Juli 2000 in Kraft.\nCopyright © 2000 by SIA Zürich"
    assert extract_valid_from(text) == "2000-07-01"


def test_extract_valid_from_prefers_explicit_date_over_designation_year():
    """Ein Dokument kann beide Muster enthalten (z.B. SIA 4009:2021 UND "Gültig ab:
    2021-08-01") -- das genauere Tagesdatum hat Vorrang."""
    from scanner.norms import extract_valid_from

    text = "SIA 4009:2021 Bauwesen\nGültig ab: 2021-08-01\nHerausgeber"
    assert extract_valid_from(text) == "2021-08-01"


def test_extract_valid_from_none_when_no_pattern_matches():
    from scanner.norms import extract_valid_from

    assert extract_valid_from("Irgendein Dokument ohne jedes Datumsmuster") is None
    assert extract_valid_from(None) is None
    assert extract_valid_from("") is None


def test_extract_valid_from_only_scans_head_and_tail_of_document():
    """Ein zufälliges Datum mitten im Fliesstext (z.B. in einem zitierten Beispiel)
    soll nicht als Gültigkeitsdatum missverstanden werden -- nur Anfang und Ende
    des Dokuments zählen (siehe test_..._finds_date_in_closing_section für den
    Fall, dass das echte Datum am Ende steht, z.B. "Genehmigung und
    Inkrafttreten")."""
    from scanner.norms import extract_valid_from

    text = (
        "Titelseite ohne Datum. " + ("Füllsatz. " * 800)
        + "Gültig ab: 1999-01-01"
        + (" Füllsatz." * 800) + " Schlussseite ohne Datum."
    )
    assert extract_valid_from(text) is None


def test_extract_valid_from_finds_date_beyond_old_4000_char_window():
    """Praxisfall SIA 142 (Ordnung, nicht die kurzen 142i-Wegleitungen): grössere
    SIA-Ordnungen stellen dem eigentlichen Titel oft eine längere mehrsprachige
    Kopfzeile voran, wodurch "Gültig ab" erst nach den alten 4000 Zeichen kam und
    deshalb übersehen wurde."""
    from scanner.norms import extract_valid_from

    text = ("Vorspann mehrsprachig. " * 200) + "Gültig ab: 2025-08-01"
    assert len(text[:4000]) < len(text), "Testaufbau: Datum muss ausserhalb der alten 4000er-Grenze liegen"
    assert extract_valid_from(text) == "2025-08-01"


def test_guess_norm_type_finds_sia_beyond_old_2000_char_window():
    """Gleicher Praxisfall wie oben, für die Typ-Anzeige: "SIA" darf nicht
    übersehen werden, nur weil es hinter einem längeren Vorspann steht --
    sonst zeigt die Normenliste fälschlich den generischen Typ "Norm" an,
    obwohl die Klassifikation (die ein grösseres Fenster durchsucht) den
    Herausgeber längst erkannt hat."""
    from scanner.norms import guess_norm_type

    text = ("Vorspann mehrsprachig. " * 200) + "Ordnung SIA 142 für Wettbewerbe"
    assert len(text[:2000]) < len(text), "Testaufbau: SIA muss ausserhalb der alten 2000er-Grenze liegen"
    assert guess_norm_type("142.pdf", text) == "SIA"


def test_extract_valid_from_finds_date_in_closing_section():
    """Praxisfall SIA 2017 (Merkblatt): "Gültig ab" fehlt, das eigentliche
    Datum steht erst im Schlussabschnitt "Genehmigung und Inkrafttreten" ganz am
    Ende des Dokuments -- muss trotz langem Fliesstext davor gefunden werden."""
    from scanner.norms import extract_valid_from

    closing = (
        "Genehmigung und Inkrafttreten\n"
        "Das vorliegende Merkblatt SIA 2017, Erhaltungswert von Bauwerken, wurde "
        "von der Zentralkommission für Normen und Ordnungen des SIA am "
        "23. Februar 2000 genehmigt.\n"
        "Es tritt am 1. Juli 2000 in Kraft.\n"
        "Copvright @ 2000 by SIA Zürich\n"
    )
    text = "Titelseite ohne Datum. " + ("Füllsatz. " * 1000) + closing
    assert extract_valid_from(text) == "2000-07-01"


# ── guess_norm_number(): fürs Zusammensetzen der Shop-URL ─────────────────────

def test_guess_norm_number_from_plain_filename():
    from scanner.norms import guess_norm_number

    assert guess_norm_number("400.pdf", None) == "400"
    assert guess_norm_number("4009.pdf", None) == "4009"


def test_guess_norm_number_with_dotted_number():
    from scanner.norms import guess_norm_number

    assert guess_norm_number("180.081.pdf", None) == "180.081"


def test_guess_norm_number_from_vss_style_filename():
    from scanner.norms import guess_norm_number

    assert guess_norm_number("SN_640050_Grundstückzufahrten.pdf", None) == "640050"


def test_guess_norm_number_falls_back_to_text():
    from scanner.norms import guess_norm_number

    assert guess_norm_number("Titelblatt.pdf", "Diese Norm SIA 400 regelt...") == "400"


def test_guess_norm_number_none_when_nothing_found():
    from scanner.norms import guess_norm_number

    assert guess_norm_number("Titelblatt.pdf", "Kein Zahlenmuster hier") is None
