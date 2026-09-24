"""Tests für die Zerlegung von Suchbegriffen (web/main.py::_make_fts_query).

Anlass: eine Suche nach „pfosten riegel system u-wert" brach mit der rohen
SQLite-Meldung »no such column: wert« ab. FTS5 liest den Bindestrich als
Spaltenfilter. Die frühere Bereinigung zählte verbotene Zeichen einzeln auf und
übersah ihn — deshalb jetzt eine Positivliste: alles ausser Buchstaben und Ziffern
trennt Wörter, genau wie beim Indexieren (unicode61)."""
import re

import pytest

from web.main import _make_fts_query, _suchfehler


@pytest.mark.parametrize("eingabe", [
    "u-wert", "pfosten riegel system u-wert", "SIA 380/1", "Kosten: 1.5 Mio.",
    "Haus (Neubau)", "a+b", "x^2", '"zitat"', "1:50", "06_Felix", "Büro & Co.",
    "was?", "50%", "#tag", "a--b", "-", "---",
])
def test_erzeugt_gueltige_fts_abfrage(tmp_db, eingabe):
    """Jede Eingabe muss eine Abfrage ergeben, die FTS5 auch annimmt."""
    ausdruck = _make_fts_query(eingabe)
    # Direkt gegen eine echte FTS5-Tabelle: nur so zeigt sich ein Syntaxfehler.
    tmp_db.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?", (ausdruck,))


def test_bindestrich_trennt_wie_beim_indexieren(tmp_db):
    """„U-Wert" steht im Index als zwei Tokens — die Suche muss genauso zerlegen,
    sonst findet sie es nie."""
    ausdruck = _make_fts_query("u-wert")
    assert "u*" in ausdruck and "wert*" in ausdruck
    assert "u-wert" not in ausdruck
    # Die Komposita-Heuristik deckt zusätzlich die Schreibweise „Uwert" ab
    assert "uwert*" in ausdruck


def test_unterstrich_trennt_ebenfalls(tmp_db):
    """Der Indexer zerlegt „06_Felix" in „06" und „felix"."""
    ausdruck = _make_fts_query("06_Felix")
    assert "06*" in ausdruck and "Felix*" in ausdruck
    assert "06_Felix" not in ausdruck


def test_nur_sonderzeichen_ergibt_leere_abfrage(tmp_db):
    assert _make_fts_query("--- ... ///") == '""'
    tmp_db.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?", ('""',))


def test_stoppwoerter_fliegen_weiter_raus(tmp_db):
    assert _make_fts_query("das haus") == "haus*"


def test_fehlermeldung_zeigt_kein_sql(tmp_db):
    """Die rohe Meldung gehört ins Log, nicht in die Oberfläche."""
    text = _suchfehler(Exception("no such column: wert"), "u-wert")
    assert "column" not in text.lower()
    assert "sql" not in text.lower()
    assert "Suche" in text


# ── Wortgrenzen ──────────────────────────────────────────────────────────────
# Anlass: „u wert" und „u-wert" verhielten sich unterschiedlich. Die Dokumentsuche
# expandierte „u" zu „u*" und traf damit jedes Wort, das mit u beginnt; die
# Ordnersuche warf das „u" ganz weg und suchte „%wert%" als Teilstring — daher
# Treffer wie „Bewertung" und „Schalldaemmwerte".

from web.main import _wortmuster, _excerpt, _MIN_PRAEFIX_LAENGE  # noqa: E402


def test_kurze_woerter_werden_nicht_zu_praefixen(tmp_db):
    """„u*" träfe und, uns, unten — eine Suche nach U-Wert wäre damit wertlos."""
    ausdruck = _make_fts_query("u wert")
    und_teil = ausdruck.split(")")[0].lstrip("(")     # nur der UND-Zweig
    assert und_teil == "u AND wert*"                  # "u" exakt, "wert" als Wortanfang


def test_beide_schreibweisen_ergeben_dieselbe_abfrage(tmp_db):
    assert _make_fts_query("u-wert") == _make_fts_query("u wert")


@pytest.mark.parametrize("wort,text,erwartet", [
    ("u", "U-Wert", True),
    ("u", "und", False),                    # kurzes Wort nur als ganzes Wort
    ("u", "250813_U-Wert", True),           # Unterstrich trennt, wie beim Indexieren
    ("wert", "U-Wert", True),
    ("wert", "Werte", True),                # längeres Wort darf Wortanfang sein
    ("wert", "Bewertung", False),           # aber nicht mitten im Wort
    ("wert", "Schalldaemmwerte", False),
    ("attika", "250813_Attika Windfang", True),
])
def test_wortgrenze_folgt_dem_indexer(tmp_db, wort, text, erwartet):
    assert bool(_wortmuster(wort).search(text)) is erwartet


def test_hervorhebung_trifft_die_fundstelle(tmp_db):
    markiert = _excerpt("Der U-Wert der Fassade betraegt 0.15 W/m2K.", "u wert")
    assert "<mark>U</mark>" in markiert and "<mark>Wert</mark>" in markiert


def test_hervorhebung_markiert_keine_zufallstreffer(tmp_db):
    """Vorher wurde bei „u wert" das erste beliebige u im Text markiert."""
    assert "<mark>" not in _excerpt("Eine Bewertung und Beurteilung folgt.", "u wert")
