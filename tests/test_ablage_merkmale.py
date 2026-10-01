import pytest

from config import settings
from scanner.ablage import merkmale as mk
from scanner.ablage.merkmale import bkp_woerter_aus_ordnern, hat_index, merkmale, stamm, typ


def keys(name, **kw):
    return {k for k in merkmale(name, **kw) if not k.startswith("tok:")}


# ── BKP gegen Projektnummer ────────────────────────────────────────────────────

def test_bkp_im_dateinamen_ist_keine_projektnummer():
    k = keys("BKP_211_Baumeister_Offerte.pdf")
    assert "bkp:211" in k and "doktyp:offerte" in k
    assert not any(x.startswith("proj:") for x in k)


def test_bkp_unterposition_und_bkp_kontext_ohne_trenner():
    assert "bkp:221" in keys("BKP 221.1 Fenster.pdf")
    assert "bkp:273" in keys("bkp273_Tueren.pdf")


def test_fuehrende_projektnummer():
    assert "proj:211" in keys("211_Emmenhof_Ansicht West.pdf")
    assert "proj:211" in keys("211 BH Schlussbesprechung.docx")


def test_projektnummer_nach_datum():
    assert "proj:215" in keys("260320_215_WB.vwx")
    assert "proj:211" in keys("250718_211_Emmenhof AT1.pdf")


def test_datum_allein_ist_keine_projektnummer():
    assert not any(x.startswith("proj:") for x in keys("260320_Stand.pdf"))


def test_fachplaner_nummer_ohne_projekt_wird_ignoriert():
    # 4-stellige Auftragsnummer am Anfang: nie ein Projekt
    assert not any(x.startswith("proj:") for x in keys("3882_Derendingen_Emmenhof-Areal_PAW-Elektro.pdf"))
    # dreistellig, aber es gibt kein Projekt 987 → ignoriert statt „unbekannt"
    assert not any(x.startswith("proj:") for x in keys("987_Elektro_Schema.pdf", bekannte_projekte={"211", "215"}))
    assert "proj:211" in keys("211_Elektro_Schema.pdf", bekannte_projekte={"211", "215"})


def test_projektnummer_regex_konfigurierbar(monkeypatch):
    monkeypatch.setattr(settings, "_settings", {**settings._load(), "ablage": {"dateiname_projektnummer": r"\d{4}"}})
    mk._wb_cached.cache_clear()
    try:
        assert "proj:1234" in keys("1234_Haus.pdf")
        assert not any(x.startswith("proj:") for x in keys("211_Haus.pdf"))
    finally:
        mk._wb_cached.cache_clear()


# ── BKP-Bezeichnungen lernen ───────────────────────────────────────────────────

def test_bkp_bezeichnungen_aus_ordnern_gelernt():
    woerter = bkp_woerter_aus_ordnern([
        ("baumeisterarbeiten", '["211"]'),
        ("gipserarbeiten", '["271"]'),
        ("spenglerarbeiten", '["222"]'),
        ("spenglerarbeiten blitzschutz bedachungsarbeiten", '["222","223","224"]'),
        ("baugrubenaushub rodungen demontagen", '["201-209"]'),    # Bereich → ignoriert
        ("tueren in holz", '["273.4"]'),
    ])
    assert woerter["baumeisterarbeiten"] == "211"
    assert woerter["spenglerarbeiten"] == "222"
    assert "baugrubenaushub" not in woerter
    wb = mk.woerterbuch().mit_bkp_woertern(woerter)
    assert "bkp:271" in merkmale("Offerte Gipserarbeiten Wohnung.pdf", wb=wb)
    assert "bkp:271" not in merkmale("Offerte Gipserarbeiten Wohnung.pdf")


def test_mehrdeutiges_bkp_wort_faellt_weg():
    woerter = bkp_woerter_aus_ordnern([("montagebau", '["212"]'), ("montagebau", '["214"]')])
    assert "montagebau" not in woerter


# ── Index / Revision / Stamm ───────────────────────────────────────────────────

@pytest.mark.parametrize("name,erwartet", [
    ("211_GR_EG_b.pdf", True), ("Plan_B.pdf", True), ("Grundriss Rev3.pdf", True),
    ("Grundriss Index C.pdf", True), ("Fassade v2.pdf", True), ("S0.1-Situation 1000 V1.pdf", True),
    ("Grundriss EG.pdf", False), ("2.5.3 Regelgeschoss Haus A.pdf", False), ("Haus B.pdf", False), ("Baubeschrieb.docx", False), ("211_Emmenhof.pdf", False),
])
def test_index_erkennung(name, erwartet):
    assert hat_index(name) is erwartet
    assert ("index" in merkmale(name)) is erwartet


def test_stamm_vorgaenger():
    assert stamm("211_GR_EG_b.pdf") == stamm("211_GR_EG_c.pdf") == stamm("211_GR_EG.pdf")
    assert stamm("211_GR_EG_b.pdf") != stamm("211_SN_A_b.pdf")


def test_haus_a_und_haus_b_sind_keine_versionen():
    assert stamm("Regelgeschoss Haus A.pdf") != stamm("Regelgeschoss Haus B.pdf")


def test_stamm_ohne_datum_und_kopie():
    assert stamm("250718_211_Emmenhof AT1.pdf") == stamm("260101_211_Emmenhof AT1.pdf")
    assert stamm("Grundriss (2).pdf") == stamm("Grundriss.pdf") == stamm("Grundriss Kopie.pdf")
    assert stamm("Plan copy.pdf") == stamm("Plan.pdf")
    assert stamm("Grundriss_2026.04.01.pdf") == stamm("Grundriss.pdf")


# ── Datum ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "260320_Stand.pdf", "20260320_Stand.pdf", "Stand 20.03.2026.pdf", "Stand_2026-03-20.pdf",
    "1_200_2026.04.01.pdf",
])
def test_datum_formate(name):
    assert "datum" in merkmale(name)


@pytest.mark.parametrize("name", ["211_Emmenhof.pdf", "Offerte 123456.pdf", "Plan_991399.pdf"])
def test_kein_datum(name):
    assert "datum" not in merkmale(name)


# ── Plantyp, Geschoss, Massstab ────────────────────────────────────────────────

def test_plantyp_langform_immer():
    assert "plantyp:GR" in merkmale("Grundrisse.pdf")
    assert "plantyp:SN" in merkmale("Schnitte 1-50.pdf")
    assert "plantyp:SIT" in merkmale("S3-Situation Baugesetz und Info 500.pdf")


def test_kurzkuerzel_nur_mit_plankontext():
    assert "plantyp:GR" in merkmale("211_GR_EG_b.pdf")            # Geschoss liefert den Kontext
    assert "plantyp:AN" in merkmale("An_West_1-100.dwg")
    assert "plantyp:AN" not in merkmale("Brief an Bauherr.docx")  # „an" ist ein deutsches Wort
    assert "plantyp:DET" not in merkmale("Det Bericht.pdf")


def test_geschoss():
    assert "geschoss:1og" in merkmale("A_07-Analyse Wohnungsverteilung 1OG.pdf")
    assert "geschoss:2og" in merkmale("Grundriss 2.OG.pdf")
    assert "geschoss:eg" in merkmale("Grundriss EG.pdf")
    assert "geschoss:attika" in merkmale("Grundriss Attika.pdf")
    # Geschoss nur mit Plan-Kontext: „EG" in einem Brief ist keins
    assert not any(k.startswith("geschoss:") for k in merkmale("Brief an EG Eigentuemer.docx"))


def test_massstab():
    assert "massstab" in merkmale("Grundriss EG 1:50.pdf")
    assert "massstab" in merkmale("Fassade M100.dwg")
    assert "massstab" in merkmale("Flurhofstrasse SG 1_200_2026.04.01.pdf")
    assert "massstab" in merkmale("RG Bestand 500 Grundriss.pdf")
    assert "massstab" not in merkmale("Rechnung 1_2024.pdf")
    assert "massstab" not in merkmale("Offerte 500.pdf")          # Zahl ohne Plan-Kontext


# ── Phase, Dokumenttyp, Fachplaner ─────────────────────────────────────────────

def test_phase():
    assert "phase:WB" in merkmale("260320_215_WB.vwx")
    assert "phase:BG" in merkmale("Baueingabe Plansatz.pdf")
    assert "phase:VP" in merkmale("Kostenschaetzung VP.xlsx")


def test_doktyp():
    assert "doktyp:protokoll" in merkmale("Protokoll BHS 12.03.2026.docx")
    assert "doktyp:protokoll" in merkmale("Bauherrenprotokoll_12.pdf")        # Bestandteil
    assert "doktyp:rechnung" in merkmale("Rechnung 4711.pdf")
    assert "doktyp:vertrag" in merkmale("Werkvertrag Baumeister.pdf")
    assert "doktyp:kv" in merkmale("KV Rohbau.xlsx")
    assert "doktyp:adressliste" in merkmale("Adressliste Projekt.xlsx")
    assert "doktyp:terminprogramm" in merkmale("Terminprogramm_v3.pdf")
    assert not any(k.startswith("doktyp:") for k in merkmale("Emmenhof.pdf"))


def test_fachplaner():
    assert "fp:bauingenieur" in merkmale("Statik Bericht.pdf")
    assert "fp:hlks" in merkmale("HLSK Schema.pdf")
    assert "fp:elektro" in merkmale("3882_PAW-Elektro.pdf")
    assert "fp:geologe" in merkmale("Baugrundgutachten Geologe.pdf")


def test_buero_woerterbuch_ergaenzt(monkeypatch):
    monkeypatch.setattr(settings, "_settings", {**settings._load(), "ablage": {
        "woerterbuch": {"doktyp": {"submission": ["subm"]}, "fachplaner": {"bauingenieur": ["tragwerksplaner"]}}}})
    mk._wb_cached.cache_clear()
    try:
        assert "doktyp:submission" in merkmale("Subm Rohbau.pdf")
        assert "fp:bauingenieur" in merkmale("Tragwerksplaner Bericht.pdf")
        assert "doktyp:protokoll" in merkmale("Protokoll.pdf")                  # Standard bleibt
    finally:
        mk._wb_cached.cache_clear()


# ── Tokens, Endung, Mail, Inhalt ───────────────────────────────────────────────

def test_tokens_und_endung():
    m = merkmale("211_Emmenhof_Ansicht West 2.pdf")
    assert m["ext:.pdf"] == 1.0
    assert {"tok:emmenhof", "tok:ansicht", "tok:west"} <= set(m)
    assert "tok:2" not in m and "tok:211" not in m             # Zahlen raus
    assert "tok:und" not in merkmale("Plan und Schnitt.pdf")    # Stoppwort
    assert "tok:umlaut" in merkmale("Ümlaut.pdf") or "tok:uemlaut" in merkmale("Ümlaut.pdf")


def test_endung_ohne_punkt_im_namen():
    assert "ext:.pdf" in merkmale("Plan 1.5 final.pdf")
    assert not any(k.startswith("ext:") for k in merkmale("Re: Projektwettbewerb Flurhofstrasse St. Gallen"))


def test_mail_domain_und_betreff():
    mail = {"sender": "Daniel Rietmann <info@danielrietmann.ch", "subject": "AW: Offerte Elektroplanung"}
    m = merkmale("AW: Offerte Elektroplanung", mail=mail)
    assert m["dom:danielrietmann.ch"] == 1.0
    assert "ext:.eml" in m
    assert "doktyp:offerte" in m and "fp:elektro" in m
    assert "tok:elektroplanung" in m


def test_inhalt_nur_erste_3000_zeichen():
    text = "Baubeschrieb Fassade " + ("x " * 2000) + " Geheimwort"
    m = merkmale("a.pdf", text=text)
    assert "inh:baubeschrieb" in m and "inh:fassade" in m
    assert "inh:geheimwort" not in m
    assert not any(k.startswith("inh:") for k in merkmale("a.pdf"))


def test_typ():
    assert typ("tok:abc") == "tok" and typ("plantyp:GR") == "plantyp"
    assert typ("index") == "flag" and typ("datum") == "flag"


def test_deterministisch():
    a = merkmale("211_GR_EG_b.pdf")
    assert a == merkmale("211_GR_EG_b.pdf")
