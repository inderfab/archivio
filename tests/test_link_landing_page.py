"""Tests für die /link-Zwischenseite in shared/menubar_bridge.py.

Hintergrund: Der per Quick Action ("Archivio-Link kopieren") kopierte Link zeigte
früher direkt auf /open?path=... -- ein simples GET auf diese URL öffnet die Datei
sofort. Fügt man so einen Link in Mail.app ein, holt Mail beim Einfügen automatisch
eine Link-Vorschau (GET), was die Datei ungewollt öffnete. /link zeigt jetzt
stattdessen eine harmlose Zwischenseite mit einem Button -- die Datei wird NUR beim
tatsächlichen Klick geöffnet, nie durch das blosse Laden/Vorschauen der Seite."""
import json
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


def _start_test_server(monkeypatch, link_action_provider=None, direct_open_provider=None):
    """Startet den echten Handler auf einem freien Port, patcht subprocess.run so
    dass wir zaehlen koennen, ob/wie oft eine Datei-Aktion tatsaechlich ausgefuehrt
    wurde -- das ist der sicherheitsrelevante Teil dieses Tests."""
    calls = []
    monkeypatch.setattr(
        bridge.subprocess, "run",
        lambda args, **kwargs: calls.append(args) or type("R", (), {"returncode": 0})(),
    )
    handler_cls = bridge.make_local_http_handler(
        "Test", log, link_action_provider=link_action_provider,
        direct_open_provider=direct_open_provider,
    )
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_port
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)
    return srv, port, calls


def test_link_landing_page_does_not_open_file(monkeypatch, tmp_path):
    """Der eigentliche Sicherheits-Fix: GET /link darf NIEMALS subprocess.run (open/
    open -R) auslösen, egal wie oft/von wem abgerufen -- genau das simuliert ein
    automatisches Link-Preview/Unfurling."""
    target = tmp_path / "plan.pdf"
    target.write_text("dummy")
    srv, port, calls = _start_test_server(monkeypatch)
    try:
        for _ in range(3):  # mehrfacher Abruf simuliert wiederholtes Preview-Fetching
            r = requests.get(f"http://127.0.0.1:{port}/link", params={"path": str(target)})
            assert r.status_code == 200
            assert "text/html" in r.headers.get("Content-Type", "")
        assert calls == [], f"GET /link hat eine Datei-Aktion ausgelöst: {calls}"
    finally:
        srv.shutdown()


def test_link_landing_page_button_points_to_open_by_default(monkeypatch, tmp_path):
    target = tmp_path / "plan.pdf"
    srv, port, calls = _start_test_server(monkeypatch)
    try:
        r = requests.get(f"http://127.0.0.1:{port}/link", params={"path": str(target)})
        assert f'href="/open?path=' in r.text
        assert "Datei öffnen" in r.text
    finally:
        srv.shutdown()


def test_link_landing_page_respects_reveal_preference(monkeypatch, tmp_path):
    target = tmp_path / "plan.pdf"
    srv, port, calls = _start_test_server(monkeypatch, link_action_provider=lambda: "reveal")
    try:
        r = requests.get(f"http://127.0.0.1:{port}/link", params={"path": str(target)})
        assert f'href="/reveal?path=' in r.text
        assert "Im Finder zeigen" in r.text
    finally:
        srv.shutdown()


def test_link_landing_page_shows_both_options_open_default(monkeypatch, tmp_path):
    """Beide Optionen müssen immer vorhanden sein -- die Menü-Präferenz bestimmt
    nur, welche als Primär-Button hervorgehoben ist."""
    target = tmp_path / "plan.pdf"
    srv, port, calls = _start_test_server(monkeypatch)
    try:
        r = requests.get(f"http://127.0.0.1:{port}/link", params={"path": str(target)})
        assert 'href="/open?path=' in r.text
        assert 'href="/reveal?path=' in r.text
        assert "Datei öffnen" in r.text
        assert "Im Finder zeigen" in r.text
    finally:
        srv.shutdown()


def test_link_landing_page_shows_both_options_reveal_default(monkeypatch, tmp_path):
    target = tmp_path / "plan.pdf"
    srv, port, calls = _start_test_server(monkeypatch, link_action_provider=lambda: "reveal")
    try:
        r = requests.get(f"http://127.0.0.1:{port}/link", params={"path": str(target)})
        assert 'href="/open?path=' in r.text
        assert 'href="/reveal?path=' in r.text
        assert "Datei öffnen" in r.text
        assert "Im Finder zeigen" in r.text
    finally:
        srv.shutdown()


def test_link_landing_page_title_shows_filename_for_link_preview(monkeypatch, tmp_path):
    """Mail.app/Slack usw. holen beim Einfuegen des Links eine Linkvorschau (Titel,
    Beschreibung) via <title>/Open-Graph-Metadaten -- die soll den Datei-/Ordnernamen
    zeigen statt der generischen Helper-Bezeichnung."""
    target = tmp_path / "Grundriss_EG.pdf"
    srv, port, calls = _start_test_server(monkeypatch)
    try:
        r = requests.get(f"http://127.0.0.1:{port}/link", params={"path": str(target)})
        assert "<title>Grundriss_EG.pdf</title>" in r.text
        assert 'property="og:title" content="Grundriss_EG.pdf"' in r.text
        assert 'property="og:site_name" content="Test"' in r.text
        assert str(tmp_path) in r.text  # Ordnerpfad als og:description
    finally:
        srv.shutdown()


def test_link_landing_page_shows_hint_about_direct_open_setting(monkeypatch, tmp_path):
    target = tmp_path / "plan.pdf"
    srv, port, calls = _start_test_server(monkeypatch)
    try:
        r = requests.get(f"http://127.0.0.1:{port}/link", params={"path": str(target)})
        assert "ohne Bestätigung öffnen" in r.text
    finally:
        srv.shutdown()


def test_link_direct_open_skips_landing_page_and_opens_immediately(monkeypatch, tmp_path):
    """Ist direct_open_provider aktiv, MUSS /link sofort die Aktion ausloesen --
    keine Zwischenseite, kein zusaetzlicher Klick noetig."""
    target = tmp_path / "plan.pdf"
    target.write_text("dummy")
    srv, port, calls = _start_test_server(monkeypatch, direct_open_provider=lambda: True)
    try:
        r = requests.get(f"http://127.0.0.1:{port}/link", params={"path": str(target)})
        assert r.status_code == 200
        assert calls == [["open", str(target)]]
    finally:
        srv.shutdown()


def test_link_direct_open_respects_reveal_preference(monkeypatch, tmp_path):
    target = tmp_path / "plan.pdf"
    target.write_text("dummy")
    srv, port, calls = _start_test_server(
        monkeypatch, link_action_provider=lambda: "reveal", direct_open_provider=lambda: True
    )
    try:
        requests.get(f"http://127.0.0.1:{port}/link", params={"path": str(target)})
        assert calls == [["open", "-R", str(target)]]
    finally:
        srv.shutdown()


def test_link_direct_open_disabled_still_shows_landing_page(monkeypatch, tmp_path):
    """direct_open_provider vorhanden, aber liefert False -- Standardverhalten
    (Bestaetigungsseite) bleibt unveraendert."""
    target = tmp_path / "plan.pdf"
    srv, port, calls = _start_test_server(monkeypatch, direct_open_provider=lambda: False)
    try:
        r = requests.get(f"http://127.0.0.1:{port}/link", params={"path": str(target)})
        assert calls == []
        assert 'href="/open?path=' in r.text
    finally:
        srv.shutdown()


def test_clicking_through_landing_page_button_actually_opens(monkeypatch, tmp_path):
    """Der tatsächliche Klick (simuliert: GET auf die im Button verlinkte /open-URL)
    MUSS weiterhin funktionieren -- der Fix darf die eigentliche Funktion nicht
    kaputt machen, nur den automatischen Preview-Trigger."""
    target = tmp_path / "plan.pdf"
    target.write_text("dummy")
    srv, port, calls = _start_test_server(monkeypatch)
    try:
        requests.get(f"http://127.0.0.1:{port}/link", params={"path": str(target)})
        assert calls == []
        r = requests.get(f"http://127.0.0.1:{port}/open", params={"path": str(target)})
        assert r.status_code == 200
        assert calls == [["open", str(target)]]
    finally:
        srv.shutdown()
