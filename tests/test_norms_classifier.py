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
