"""Tests für scanner/norm_freshness.py -- der manuelle Online-Abgleich einer
erkannten Norm gegen shop.sia.ch/mobilityplatform.ch. Alle HTTP-Aufrufe werden
gemockt -- die Tests dürfen nicht vom echten Internet abhängen."""
import pytest
import requests

from scanner import norm_freshness as nf


class _FakeResp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def test_get_with_retry_recovers_from_transient_failure(monkeypatch):
    """Ein Netzwerkfehler beim ersten Versuch darf nicht sofort aufgeben --
    Praxisfall: ein einzelner Request an shop.sia.ch schlug fehl, obwohl ein
    zweiter Versuch Sekunden später funktioniert hätte."""
    monkeypatch.setattr(nf.time, "sleep", lambda *_: None)
    calls = []

    def flaky_get(url, timeout=None, headers=None):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("boom")
        return _FakeResp(200, "ok")

    monkeypatch.setattr(nf.requests, "get", flaky_get)
    resp = nf._get_with_retry("http://example.test", retries=1)
    assert resp is not None
    assert resp.text == "ok"
    assert len(calls) == 2


def test_get_with_retry_gives_up_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(nf.time, "sleep", lambda *_: None)
    monkeypatch.setattr(nf.requests, "get", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("boom")))
    assert nf._get_with_retry("http://example.test", retries=1) is None


def test_get_with_retry_raises_offline_error_without_retry_delay(monkeypatch):
    """Praxisfall: kein Internetzugang (z.B. Archivio bewusst komplett offline
    betrieben). Ein requests.exceptions.ConnectionError kommt praktisch sofort
    (DNS-Fehler/Verbindung abgelehnt) und ist NICHT transient wie ein Timeout --
    ein erneuter Versuch mit Wartezeit würde nur unnötig Zeit kosten, ohne je
    erfolgreich zu sein. Muss deshalb sofort als _OfflineError durchgereicht
    werden, ohne zu schlafen oder einen zweiten Versuch zu unternehmen."""
    calls = []
    sleeps = []
    monkeypatch.setattr(nf.time, "sleep", lambda s: sleeps.append(s))

    def fake_get(*a, **k):
        calls.append(1)
        raise requests.exceptions.ConnectionError("Name or service not known")

    monkeypatch.setattr(nf.requests, "get", fake_get)
    with pytest.raises(nf._OfflineError):
        nf._get_with_retry("http://example.test", retries=1)
    assert len(calls) == 1, "kein zweiter Versuch bei einem echten Verbindungsfehler"
    assert sleeps == [], "keine Wartezeit vor einem sinnlosen zweiten Versuch"


def test_check_sia_current_edition_has_no_archived_marker(monkeypatch):
    def fake_get(url, timeout=None, headers=None):
        if "/ingenieur/" in url:
            return _FakeResp(200, "<p>Produktnummer SIA 103 gültig ab 01.01.2020</p>")
        return _FakeResp(404, "")

    monkeypatch.setattr(nf.requests, "get", fake_get)
    status, detail = nf.check_sia("103", "2020")
    assert status == "aktuell"
    assert "shop.sia.ch" in detail


def test_check_sia_archived_edition_detected(monkeypatch):
    def fake_get(url, timeout=None, headers=None):
        if "/ingenieur/" in url:
            return _FakeResp(
                200,
                "<p>Produktnummer SIA 103-K gültig ab 01.11.2018 gültig bis 31.12.2019, "
                "archivierter Titel!</p>",
            )
        return _FakeResp(404, "")

    monkeypatch.setattr(nf.requests, "get", fake_get)
    status, detail = nf.check_sia("103-K", "2018")
    assert status == "veraltet"


def test_check_sia_without_local_year_is_unchecked(monkeypatch):
    calls = []
    monkeypatch.setattr(nf.requests, "get", lambda *a, **k: calls.append(1) or _FakeResp(200, ""))
    status, detail = nf.check_sia("103", None)
    assert status == "ungeprüft"
    assert calls == [], "ohne Jahr darf gar kein Request ausgelöst werden"


def test_check_sia_no_matching_category_found(monkeypatch):
    monkeypatch.setattr(nf.requests, "get", lambda *a, **k: _FakeResp(404, ""))
    status, detail = nf.check_sia("999", "2020")
    assert status == "ungeprüft"


def test_check_sia_stops_immediately_when_offline(monkeypatch):
    """Praxisfall: Archivio läuft komplett offline (bewusst möglich, siehe
    Projektprinzip). Ohne Internetzugang darf check_sia() NICHT alle 5 Kategorien
    durchprobieren (jede genauso aussichtslos) -- das würde pro Norm unnötig
    Zeit kosten, bei einem Sammel-Lauf über 300+ Normen erheblich."""
    calls = []

    def fake_get(*a, **k):
        calls.append(1)
        raise requests.exceptions.ConnectionError("Network is unreachable")

    monkeypatch.setattr(nf.requests, "get", fake_get)
    status, detail = nf.check_sia("103", "2020")
    assert status == "ungeprüft"
    assert detail == nf.OFFLINE_DETAIL
    assert len(calls) == 1, "darf nicht alle 5 Kategorien durchprobieren, wenn schon die erste offline ist"


def test_check_vss_reports_offline_status(monkeypatch):
    def fake_get(*a, **k):
        raise requests.exceptions.ConnectionError("Network is unreachable")

    monkeypatch.setattr(nf.requests, "get", fake_get)
    status, detail = nf.check_vss("640050")
    assert status == "ungeprüft"
    assert detail == nf.OFFLINE_DETAIL


def test_check_norm_reports_offline_status_for_both_sources(monkeypatch):
    """Egal ob SIA oder VSS -- check_norm() darf bei fehlender Internetverbindung
    nicht crashen, sondern muss den Offline-Grund konsistent durchreichen."""
    def fake_get(*a, **k):
        raise requests.exceptions.ConnectionError("Network is unreachable")

    monkeypatch.setattr(nf.requests, "get", fake_get)
    status_sia, detail_sia = nf.check_norm("400", "2020", "SIA")
    status_vss, detail_vss = nf.check_norm("640050", None, "VSS")
    assert (status_sia, detail_sia) == ("ungeprüft", nf.OFFLINE_DETAIL)
    assert (status_vss, detail_vss) == ("ungeprüft", nf.OFFLINE_DETAIL)


def test_check_sia_wrong_product_on_page_is_rejected(monkeypatch):
    """Eine 200-Antwort allein genügt nicht -- die Seite muss auch wirklich die
    gesuchte Produktnummer nennen (sonst z.B. eine falsche Kategorie-Startseite)."""
    monkeypatch.setattr(
        nf.requests, "get",
        lambda *a, **k: _FakeResp(200, "<p>Produktnummer SIA 999 gültig ab 01.01.2020</p>"),
    )
    status, detail = nf.check_sia("103", "2020")
    assert status == "ungeprüft"


def test_check_sia_matches_slash_vs_dash_in_product_number(monkeypatch):
    """Praxisfall SIA 118-242 (Vertragsbedingungen zur Norm SIA 242): die URL
    braucht einen Bindestrich ("118-242_2012_d"), aber die Produktnummer auf der
    Seite selbst steht mit Schrägstrich ("Produktnummer SIA 118/242") -- ein
    exakter String-Vergleich verpasste dadurch den echten Treffer."""
    monkeypatch.setattr(
        nf.requests, "get",
        lambda *a, **k: _FakeResp(200, "<p>Produktnummer SIA 118/242 gültig ab 01.10.2012</p>"),
    )
    status, detail = nf.check_sia("118-242", "2012")
    assert status == "aktuell"


def test_check_vss_withdrawn_norm_detected(monkeypatch):
    html = (
        "<div>Im Webviewer anzeigen Mehr erfahren ausser Kraft VSS "
        "Dokumenten-Nummer SN-640050 alternative Dokumenten-Nummer 640050 "
        "Publikationsjahr 1993</div>"
    )
    monkeypatch.setattr(nf.requests, "get", lambda *a, **k: _FakeResp(200, html))
    status, detail = nf.check_vss("640050")
    assert status == "veraltet"
    assert "mobilityplatform.ch" in detail


def test_check_vss_active_norm_has_no_badge(monkeypatch):
    html = (
        "<div>Im Webviewer anzeigen Mehr erfahren VSS "
        "Dokumenten-Nummer SN-EN-ISO-13473-1_2022-04_DE alternative "
        "Dokumenten-Nummer 13473-1 Publikationsjahr 2022</div>"
    )
    monkeypatch.setattr(nf.requests, "get", lambda *a, **k: _FakeResp(200, html))
    status, detail = nf.check_vss("13473")
    assert status == "aktuell"


def test_check_vss_no_match_is_unchecked(monkeypatch):
    monkeypatch.setattr(nf.requests, "get", lambda *a, **k: _FakeResp(200, "<div>nichts hier</div>"))
    status, detail = nf.check_vss("640050")
    assert status == "ungeprüft"


def test_check_vss_empty_number_skips_request(monkeypatch):
    calls = []
    monkeypatch.setattr(nf.requests, "get", lambda *a, **k: calls.append(1) or _FakeResp(200, ""))
    status, detail = nf.check_vss("")
    assert status == "ungeprüft"
    assert calls == []


def test_check_norm_dispatches_by_source(monkeypatch):
    calls = []
    monkeypatch.setattr(nf, "check_sia", lambda number, year: calls.append(("sia", number, year)) or ("aktuell", "x"))
    monkeypatch.setattr(nf, "check_vss", lambda number: calls.append(("vss", number)) or ("aktuell", "x"))

    nf.check_norm("400", "2020", "SIA")
    nf.check_norm("640050", None, "VSS")
    nf.check_norm("640050", None, "SN")
    status, detail = nf.check_norm("1090", None, "EN")

    assert calls == [("sia", "400", "2020"), ("vss", "640050"), ("vss", "640050")]
    assert status == "ungeprüft"
    assert "EN" in detail


def test_check_norm_without_number_is_unchecked():
    status, detail = nf.check_norm(None, "2020", "SIA")
    assert status == "ungeprüft"


def test_check_norm_truncates_full_iso_date_to_year_for_sia(monkeypatch):
    """norm_valid_from ist ein volles ISO-Datum ("2020-01-01"), check_sia()
    braucht aber nur die Jahreszahl für die Shop-URL -- ohne diese Kürzung baute
    die URL fälschlich ".../103_2020-01-01_d/..." statt ".../103_2020_d/..."."""
    calls = []
    monkeypatch.setattr(nf, "check_sia", lambda number, year: calls.append((number, year)) or ("aktuell", "x"))
    nf.check_norm("103", "2020-01-01", "SIA")
    assert calls == [("103", "2020")]
