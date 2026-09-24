"""Tests für die Startseite auf Port 8000 (shared/menubar_bridge.py).

Zwischen „Menüleisten-App gestartet" und „uvicorn nimmt Verbindungen an" liegen nach
einer Neuinstallation bis zu einer Minute — Datenverzeichnis anlegen, Python-Bundle,
schwere Importe. Wer in dieser Zeit den Browser öffnet, sah nur „Verbindung
fehlgeschlagen" und hielt Archivio für kaputt.

Kritisch ist das saubere Freigeben des Ports: bleibt die Startseite hängen, kann
uvicorn danach nicht mehr binden und der Server startet überhaupt nicht."""
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "shared"))
import menubar_bridge as bridge  # noqa: E402


def _freier_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def port():
    p = _freier_port()
    yield p
    bridge.startseite_aus(None)


def test_zeigt_meldung_statt_toter_verbindung(port):
    bridge.startseite_an(port, None)
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3)
        pytest.fail("erwartet wurde 503")
    except urllib.error.HTTPError as e:
        assert e.code == 503
        text = e.read().decode("utf-8")
        assert "Archivio startet" in text
        assert "eine Minute dauern" in text


def test_antwortet_auf_jedem_pfad(port):
    """Egal welche Adresse der Nutzer aufruft — Dashboard, Einstellungen, Suche."""
    bridge.startseite_an(port, None)
    for pfad in ("/", "/dashboard", "/dashboard/settings", "/api/status"):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}{pfad}", timeout=3)
            pytest.fail("erwartet wurde 503")
        except urllib.error.HTTPError as e:
            assert e.code == 503


def test_gibt_den_port_wieder_frei(port):
    """Der wichtigste Fall: bleibt der Port belegt, kann uvicorn nicht starten."""
    bridge.startseite_an(port, None)
    bridge.startseite_aus(None)

    # Muss sich sofort neu binden lassen
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
    finally:
        s.close()


def test_zweimal_einschalten_ist_harmlos(port):
    bridge.startseite_an(port, None)
    bridge.startseite_an(port, None)     # darf nicht werfen
    bridge.startseite_aus(None)
    bridge.startseite_aus(None)          # ebenso


def test_belegter_port_wirft_nicht(port):
    """Läuft dort schon etwas, wird die Startseite still übersprungen statt den
    Start der ganzen App zu verhindern."""
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("0.0.0.0", port))
    blocker.listen(1)
    try:
        bridge.startseite_an(port, None)     # darf nicht werfen
    finally:
        blocker.close()
