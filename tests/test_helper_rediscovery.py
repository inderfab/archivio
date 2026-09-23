"""Tests für die automatische Server-Neusuche des Helpers
(shared/menubar_bridge.py::pick_rediscovered_server).

Hintergrund: zieht der Server auf einen anderen Mac, zeigen alle Arbeitsplätze
weiter auf die alte Adresse. Die Suche beim Start greift dort nicht, weil sie nur
läuft, solange die Adresse noch der Auslieferungszustand ist. Die Neusuche
überschreibt also eine vom Nutzer gesetzte Adresse -- deshalb darf sie nur in einem
eindeutigen, überprüften Fall zuschlagen."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "shared"))
import menubar_bridge as bridge  # noqa: E402

_ERREICHBAR = lambda url: True
_TOT = lambda url: False


def test_switches_to_single_reachable_server():
    url = bridge.pick_rediscovered_server(
        [("192.168.1.50", 8000)], "http://192.168.1.10:8000", _ERREICHBAR)
    assert url == "http://192.168.1.50:8000"


def test_no_switch_when_nothing_found():
    assert bridge.pick_rediscovered_server([], "http://192.168.1.10:8000", _ERREICHBAR) is None


def test_no_switch_when_several_found():
    """Bei mehreren Servern im Netz (z.B. ein Testrechner) darf nicht geraten werden."""
    found = [("192.168.1.50", 8000), ("192.168.1.51", 8000)]
    assert bridge.pick_rediscovered_server(found, "http://192.168.1.10:8000", _ERREICHBAR) is None


def test_no_switch_to_the_same_address():
    assert bridge.pick_rediscovered_server(
        [("192.168.1.10", 8000)], "http://192.168.1.10:8000", _ERREICHBAR) is None
    # Ein abweichender Schrägstrich am Ende ist dieselbe Adresse
    assert bridge.pick_rediscovered_server(
        [("192.168.1.10", 8000)], "http://192.168.1.10:8000/", _ERREICHBAR) is None


def test_no_switch_to_stale_mdns_entry():
    """mDNS meldet auch Server, die längst aus sind -- ohne Gegenprobe würde der
    Helper auf eine tote Adresse wechseln und wäre schlechter dran als vorher."""
    assert bridge.pick_rediscovered_server(
        [("192.168.1.50", 8000)], "http://192.168.1.10:8000", _TOT) is None


def test_probe_error_does_not_switch():
    def kaputt(url):
        raise OSError("Netzwerk weg")

    assert bridge.pick_rediscovered_server(
        [("192.168.1.50", 8000)], "http://192.168.1.10:8000", kaputt) is None
