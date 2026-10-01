import unicodedata
from pathlib import Path

import pytest

from scanner.ablage.normalisieren import (
    art_bestimmen, ist_trenner, label_pfad, pfad_schluessel, zerlege,
)

FIXTURE = Path(__file__).parent / "fixtures" / "musterordner_strut.txt"


def _fixture_pfade() -> list[str]:
    return FIXTURE.read_text(encoding="utf-8").splitlines()


# Name → (Präfix, Label, Codes)
FAELLE = [
    ("51a_Planstände", ("51a", "planstaende", [])),
    ("51_Bauingenieur", ("51", "bauingenieur", [])),
    ("22_Wettbewerb_Studienauftrag", ("22", "wettbewerb studienauftrag", [])),
    ("31_Vorprojekt", ("31", "vorprojekt", [])),
    ("33_Baubewilligungsverfahren", ("33", "baubewilligungsverfahren", [])),
    ("53_Inbetriebnahme_Revision", ("53", "inbetriebnahme revision", [])),
    ("31d_Fachplaner", ("31d", "fachplaner", [])),
    ("33h_Fachplaner", ("33h", "fachplaner", [])),
    ("51f_Fachplaner", ("51f", "fachplaner", [])),
    ("31_HLSK-Ingenieur", ("31", "hlsk ingenieur", [])),
    ("33_Geologe - Altlasten", ("33", "geologe altlasten", [])),
    ("1.6_HLSK-Ingenieur", ("1.6", "hlsk ingenieur", [])),
    ("1.0_alle_Fachplaner", ("1.0", "alle fachplaner", [])),
    ("33.1_Plaene", ("33.1", "plaene", [])),
    ("01_Kontrollplaene", ("01", "kontrollplaene", [])),
    ("00_in Bearbeitung", ("00", "in bearbeitung", [])),
    ("00_in_Bearbeitung", ("00", "in bearbeitung", [])),
    ("00_abgeschlossen", ("00", "abgeschlossen", [])),
    ("00_ungueltig", ("00", "ungueltig", [])),
    ("0 CAD Daten und Export", ("0", "cad daten und export", [])),
    ("000_Submittentenliste", ("000", "submittentenliste", [])),
    ("a_Projektorganisation", ("a", "projektorganisation", [])),
    ("h_Projektraum", ("h", "projektraum", [])),
    ("z_Archiv", ("z", "archiv", [])),
    ("Z_Archiv", ("Z", "archiv", [])),
    ("z_weitere", ("z", "weitere", [])),
    ("2_upload", ("2", "upload", [])),
    ("Baubeschrieb", ("", "baubeschrieb", [])),
    ("Skizzen", ("", "skizzen", [])),
    ("Farb_Materialkonzept", ("", "farb materialkonzept", [])),
    ("Farb_Materialkonzept > evt. löschen da im CAD-Dokument enthalten?",
     ("", "farb materialkonzept", [])),
    ("4_Planverwaltung_Nebenkosten - PAUSCHAL", ("4", "planverwaltung nebenkosten pauschal", [])),
    # BKP
    ("211_Baumeisterarbeiten", ("", "baumeisterarbeiten", ["211"])),
    ("201_209_Baugrubenaushub_Rodungen_Demontagen",
     ("", "baugrubenaushub rodungen demontagen", ["201-209"])),
    ("221_1_Fenster_aus_Holz_Metall", ("", "fenster aus holz metall", ["221.1"])),
    ("213.6 + 215.6 Montagebau in Stahl + Metall",
     ("", "montagebau in stahl metall", ["213.6", "215.6"])),
    ("222-223-224 Spenglerarbeiten-Blitzschutz-Bedachungsarbeiten",
     ("", "spenglerarbeiten blitzschutz bedachungsarbeiten", ["222", "223", "224"])),
    ("258 + 358 Kücheneinrichtungen", ("", "kuecheneinrichtungen", ["258", "358"])),
    ("258_Kuecheneinrichtungen", ("", "kuecheneinrichtungen", ["258"])),
    ("273.4 Türen in Holz", ("", "tueren in holz", ["273.4"])),
    ("273_4_Wohnungstueren", ("", "wohnungstueren", ["273.4"])),
    ("282_283 Wandbelaege_Wand_Deckenbekleidungen",
     ("", "wandbelaege wand deckenbekleidungen", ["282-283"])),
    ("139 Baustelleneinrichtungen", ("", "baustelleneinrichtungen", ["139"])),
    ("230 Schrankensystem ESH", ("", "schrankensystem esh", ["230"])),
    ("401_499_Umgebung", ("", "umgebung", ["401-499"])),
    # Projektnummer-Präfixe der CAD-Export-Ordner (Bushof): Plannummer-Ziffer wird Präfix
    ("182_51 Ausführung", ("", "ausfuehrung", ["182.51"])),
    ("182_51_2 Geschosse", ("2", "geschosse", ["182.51"])),
    ("182_51_1 Situation + Umgebung", ("1", "situation umgebung", ["182.51"])),
    # Datum ist kein Code und kein Präfix
    ("250813_Attika", ("", "250813 attika", [])),
]


@pytest.mark.parametrize("name,erwartet", FAELLE, ids=[f[0] for f in FAELLE])
def test_zerlege_tabelle(name, erwartet):
    assert tuple(zerlege(name)) == erwartet


def test_mindestens_40_faelle():
    assert len(FAELLE) >= 40


@pytest.mark.parametrize("name,_", FAELLE[:60], ids=[f[0] for f in FAELLE[:60]])
def test_nfd_gleich_nfc(name, _):
    assert zerlege(unicodedata.normalize("NFD", name)) == zerlege(unicodedata.normalize("NFC", name))


def test_planstaende_umlaut_und_ae_gleich():
    assert zerlege("51a_Planstände").label == zerlege("51a_Planstaende").label == "planstaende"
    assert zerlege("Bodenbeläge").label == zerlege("Bodenbelaege").label


def test_bekannte_inkonsistenzen_ergeben_gleiches_label():
    paare = [
        ("00_in Bearbeitung", "00_in_Bearbeitung"),
        ("281_Bodenbeläge_Unterlagsboden", "281_Bodenbelaege_Unterlagsboden"),
        ("Z_Archiv", "z_Archiv"),
        ("258 + 358 Kücheneinrichtungen", "258_Kuecheneinrichtungen"),
        ("276_279_Innere_Abschluess_Elementwaende", "276_279_Innere_Abschluess_Elementwaende"),
    ]
    for a, b in paare:
        assert zerlege(a).label == zerlege(b).label, (a, b)


def test_code_ist_nie_der_schluessel():
    # derselbe Code, aber zwei verschiedene Ordner
    a, b = zerlege("273.4 Türen in Holz"), zerlege("273_4_Wohnungstueren")
    assert a.codes == b.codes == ["273.4"]
    assert a.label != b.label


def test_bkp_code_wird_nicht_praefix():
    assert zerlege("211_Baumeisterarbeiten").praefix == ""


def test_trenner_und_archiv():
    for n in ("0-------", "a ------", "00-----", "-----"):
        assert ist_trenner(n), n
        assert art_bestimmen(n) == "trenner"
    assert not ist_trenner("0 CAD Daten und Export")
    assert not ist_trenner("00_in_Bearbeitung")
    assert art_bestimmen("z_Archiv") == "archiv"
    assert art_bestimmen("Z_Archiv") == "archiv"
    assert art_bestimmen("51a_Planstände") == "normal"
    assert art_bestimmen("Temp", ausgeschlossen=True) == "ausgeschlossen"


def test_ungueltig_ist_kein_archiv():
    assert art_bestimmen("00_ungueltig") == "normal"


def test_archiv_labels_konfigurierbar():
    assert art_bestimmen("Altlasten_x", archiv_labels=["altlasten x"]) == "archiv"
    assert art_bestimmen("z_Archiv", archiv_labels=["alt"]) == "normal"


def test_pfad_schluessel_nfd_nfc():
    nfd = unicodedata.normalize("NFD", "/Volumes/X/31_Vorprojekt/31a_Planstände/")
    nfc = unicodedata.normalize("NFC", "/Volumes/X/31_Vorprojekt/31a_Planstände")
    assert nfd != nfc
    assert pfad_schluessel(nfd) == pfad_schluessel(nfc) == nfc
    assert pfad_schluessel("/") == "/"


def test_label_pfad():
    assert label_pfad("51_Ausfuehrung/51a_Planstände") == "ausfuehrung/planstaende"
    assert label_pfad(unicodedata.normalize("NFD", "51_Ausfuehrung/51a_Planstände")) == "ausfuehrung/planstaende"
    assert label_pfad("0-------/22_Wettbewerb_Studienauftrag") == "wettbewerb studienauftrag"


def test_fixture_vollstaendig_und_phasen_rolle():
    pfade = _fixture_pfade()
    assert len(pfade) == 794
    labels = {}
    for p in pfade:
        for teil in p.split("/"):
            z = zerlege(teil)
            assert z.label, teil
            labels.setdefault(teil, z)
    # Planstände: gleiche Rolle in allen Phasen, obwohl Buchstabe/Zahl wechselt
    plan = {zerlege(n).label for n in labels if "Planst" in n}
    assert plan == {"planstaende"}
    fp = {zerlege(n).praefix for n in labels if n.endswith("_Fachplaner") and n[0].isdigit() and len(n) > 12}
    assert {"31d", "33h", "41e", "51f", "22d"} <= fp
    # Fachplaner-Rolle wird über alle Phasen als einziges Label erkannt
    assert {zerlege(n).label for n in labels if n.endswith("_Fachplaner") and n[0].isdigit()
            and not n.startswith(("1_", "1."))} == {"fachplaner"}


def test_fixture_slot_pfade_fassen_inkonsistenzen_zusammen():
    pfade = _fixture_pfade()
    lp = {label_pfad(p) for p in pfade}
    assert len(lp) < len(pfade)
    assert not any("planstande" in x for x in lp)
    # beide Schreibweisen von „in Bearbeitung" landen im selben Slot
    namen = {p.rsplit("/", 1)[-1] for p in pfade}
    assert {"00_in Bearbeitung", "00_in_Bearbeitung"} <= namen
    assert len({zerlege(n).label for n in ("00_in Bearbeitung", "00_in_Bearbeitung")}) == 1
    assert not any(x.endswith("z archiv") for x in lp)  # Präfix z wird abgetrennt


def test_fixture_alle_archiv_und_trenner_sinnvoll():
    namen = {t for p in _fixture_pfade() for t in p.split("/")}
    archive = {n for n in namen if art_bestimmen(n) == "archiv"}
    assert archive == {"z_Archiv", "Z_Archiv"}
    assert not any(ist_trenner(n) for n in namen if n != "0 CAD Daten und Export" and not n.strip("-"))
