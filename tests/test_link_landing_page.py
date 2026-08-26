"""Tests für /link in shared/menubar_bridge.py -- der per Quick Action ("Archivio-
Link kopieren") kopierte Link.

Verhalten (nach mehreren Regressions-Runden in beide Richtungen -- siehe Commit-
Historie): /link führt bei einem ECHTEN Browser-Klick SOFORT die Aktion aus (öffnen
bzw. im Finder zeigen, abhängig von der AKTUELLEN Helper-/Server-Menü-Einstellung der
klickenden Person) -- keine Zwischenseite, kein zweiter Klick. Ein automatisierter
Link-Vorschau-Abruf (Mail/Messages LPLinkView beim Einfügen des Links) löst dagegen
NIE eine Aktion aus, bekommt aber trotzdem die og:title-getaggte Seite mit dem
korrekten Datei-/Ordnernamen für die Vorschau-Karte.

Unterscheidung: der User-Agent-Header. Echte Browser (Safari, Chrome, Firefox, Edge)
hängen immer einen Produkt-Token wie "Safari/" an; automatisierte Metadaten-Fetcher
i.d.R. nicht -- Standardtechnik gegen genau dieses Problem (vgl. Slack-/Twitter-/
Facebook-Unfurling-Bot-Erkennung auf Websites)."""
import logging
import sys
import threading
import time
from http.server import HTTPServer
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "shared"))
import menubar_bridge as bridge  # noqa: E402

log = logging.getLogger("test")

_SAFARI_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.4 Safari/605.1.15")
# Realistischer fuer einen Metadaten-Fetcher: WebKit-basiert, aber ohne "Safari/"-Token.
_FETCHER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko)"


def _start_test_server(monkeypatch, link_action_provider=None):
    """Startet den echten Handler auf einem freien Port, patcht subprocess.run so
    dass wir zaehlen koennen, ob/wie oft eine Datei-Aktion tatsaechlich ausgefuehrt
    wurde."""
    calls = []
    monkeypatch.setattr(
        bridge.subprocess, "run",
        lambda args, **kwargs: calls.append(args) or type("R", (), {"returncode": 0})(),
    )
    handler_cls = bridge.make_local_http_handler(
        "Test", log, link_action_provider=link_action_provider,
    )
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_port
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)
    return srv, port, calls


def test_looks_like_real_browser():
    assert bridge._looks_like_real_browser(_SAFARI_UA) is True
    assert bridge._looks_like_real_browser(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36") is True
    assert bridge._looks_like_real_browser(_FETCHER_UA) is False
    assert bridge._looks_like_real_browser("") is False
    assert bridge._looks_like_real_browser(None) is False


def test_link_with_browser_user_agent_opens_file_immediately(monkeypatch, tmp_path):
    target = tmp_path / "plan.pdf"
    target.write_text("dummy")
    srv, port, calls = _start_test_server(monkeypatch)
    try:
        r = requests.get(f"http://127.0.0.1:{port}/link", params={"path": str(target)},
                          headers={"User-Agent": _SAFARI_UA})
        assert r.status_code == 200
        assert calls == [["open", str(target)]]
    finally:
        srv.shutdown()


def test_link_without_browser_user_agent_does_not_open_file(monkeypatch, tmp_path):
    """Der eigentliche Sicherheits-Fix: ein Link-Vorschau-Abruf (kein Browser-UA)
    darf NIEMALS subprocess.run (open/open -R) auslösen."""
    target = tmp_path / "plan.pdf"
    target.write_text("dummy")
    srv, port, calls = _start_test_server(monkeypatch)
    try:
        for _ in range(3):  # mehrfacher Abruf simuliert wiederholtes Preview-Fetching
            r = requests.get(f"http://127.0.0.1:{port}/link", params={"path": str(target)},
                              headers={"User-Agent": _FETCHER_UA})
            assert r.status_code == 200
        assert calls == [], f"Link-Vorschau-Abruf hat eine Datei-Aktion ausgelöst: {calls}"
    finally:
        srv.shutdown()


def test_link_without_user_agent_does_not_open_file(monkeypatch, tmp_path):
    """Fehlender User-Agent (viele einfache HTTP-Clients/Fetcher) gilt ebenfalls
    nicht als echter Browser-Klick."""
    target = tmp_path / "plan.pdf"
    target.write_text("dummy")
    srv, port, calls = _start_test_server(monkeypatch)
    try:
        r = requests.get(f"http://127.0.0.1:{port}/link", params={"path": str(target)},
                          headers={"User-Agent": ""})
        assert r.status_code == 200
        assert calls == []
    finally:
        srv.shutdown()


def test_link_respects_reveal_preference_for_real_browser(monkeypatch, tmp_path):
    """link_action_provider wird bei JEDEM Aufruf frisch gelesen -- entspricht der
    Einstellung der klickenden Person, nicht der kopierenden."""
    target = tmp_path / "plan.pdf"
    target.write_text("dummy")
    srv, port, calls = _start_test_server(monkeypatch, link_action_provider=lambda: "reveal")
    try:
        r = requests.get(f"http://127.0.0.1:{port}/link", params={"path": str(target)},
                          headers={"User-Agent": _SAFARI_UA})
        assert r.status_code == 200
        assert calls == [["open", "-R", str(target)]]
    finally:
        srv.shutdown()


def test_link_shows_filename_for_preview_regardless_of_user_agent(monkeypatch, tmp_path):
    """Mail.app/Messages holen beim Einfuegen des Links eine Linkvorschau (Titel,
    Beschreibung) via <title>/Open-Graph-Metadaten -- die soll den Datei-/Ordnernamen
    zeigen statt der generischen Helper-Bezeichnung, UNABHAENGIG davon ob die Aktion
    ausgeloest wurde oder nicht."""
    target = tmp_path / "Grundriss_EG.pdf"
    target.write_text("dummy")
    srv, port, calls = _start_test_server(monkeypatch)
    try:
        for ua in (_SAFARI_UA, _FETCHER_UA):
            r = requests.get(f"http://127.0.0.1:{port}/link", params={"path": str(target)},
                              headers={"User-Agent": ua})
            assert "<title>Grundriss_EG.pdf</title>" in r.text
            assert 'property="og:title" content="Grundriss_EG.pdf"' in r.text
            assert str(tmp_path) in r.text  # Ordnerpfad als og:description
    finally:
        srv.shutdown()


def test_link_missing_file_shows_not_found_for_real_browser(monkeypatch, tmp_path):
    target = tmp_path / "missing.pdf"
    srv, port, calls = _start_test_server(monkeypatch)
    try:
        r = requests.get(f"http://127.0.0.1:{port}/link", params={"path": str(target)},
                          headers={"User-Agent": _SAFARI_UA})
        assert r.status_code == 404
        assert calls == []
    finally:
        srv.shutdown()


def test_open_and_reveal_routes_still_work_directly(monkeypatch, tmp_path):
    """/open und /reveal bleiben als direkte Routen bestehen (z.B. für Buttons in
    den Suchergebnissen) -- unabhängig von /link, dessen Menü-Präferenz und der
    User-Agent-Pruefung (die gilt nur für /link)."""
    target = tmp_path / "plan.pdf"
    target.write_text("dummy")
    srv, port, calls = _start_test_server(monkeypatch)
    try:
        r = requests.get(f"http://127.0.0.1:{port}/open", params={"path": str(target)},
                          headers={"User-Agent": _FETCHER_UA})
        assert r.status_code == 200
        assert calls == [["open", str(target)]]
        r = requests.get(f"http://127.0.0.1:{port}/reveal", params={"path": str(target)},
                          headers={"User-Agent": _FETCHER_UA})
        assert calls[-1] == ["open", "-R", str(target)]
    finally:
        srv.shutdown()
