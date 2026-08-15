"""Tests für ensure_autostart_default()/save_autostart_preference() in
shared/menubar_bridge.py. Grund: eine .pkg-Neuinstallation ersetzt den kompletten
.app-Ordner -- macOS' Login-Item-Mechanismus verwirft dabei den Alias auf die alte
Datei, autostart_enabled() faellt danach stillschweigend auf False zurueck, obwohl der
Nutzer nichts geaendert hat. Die Praeferenz wird deshalb separat in einer JSON-Datei
persistiert und bei jedem Start gegen den echten macOS-Zustand abgeglichen."""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "shared"))
import menubar_bridge as bridge  # noqa: E402

log = logging.getLogger("test")


def test_fresh_install_no_state_file_defaults_to_enabled(tmp_path, monkeypatch):
    """Kein State-File (frischer Erststart) => Autostart wird standardmaessig aktiviert."""
    state_path = tmp_path / "state.json"
    calls = []
    monkeypatch.setattr(bridge, "autostart_enabled", lambda: False)
    monkeypatch.setattr(bridge, "set_autostart", lambda enabled, log: calls.append(enabled))

    result = bridge.ensure_autostart_default(log, state_path)

    assert result is True
    assert calls == [True]


def test_reinstall_wiped_login_item_gets_silently_restored(tmp_path, monkeypatch):
    """Praeferenz sagt "an", macOS-Zustand ist "aus" (durch Neuinstallation verworfen)
    => wird automatisch wiederhergestellt, kein Nutzereingriff noetig."""
    state_path = tmp_path / "state.json"
    state_path.write_text('{"autostart_preference": true}')
    calls = []
    monkeypatch.setattr(bridge, "autostart_enabled", lambda: False)
    monkeypatch.setattr(bridge, "set_autostart", lambda enabled, log: calls.append(enabled))

    result = bridge.ensure_autostart_default(log, state_path)

    assert result is True
    assert calls == [True], "Autostart haette nach Neuinstallation automatisch wiederhergestellt werden muessen"


def test_explicitly_disabled_preference_is_respected(tmp_path, monkeypatch):
    """Nutzer hat Autostart bewusst ausgeschaltet -- darf NICHT automatisch wieder
    aktiviert werden, auch wenn der macOS-Zustand "aus" ist."""
    state_path = tmp_path / "state.json"
    state_path.write_text('{"autostart_preference": false}')
    calls = []
    monkeypatch.setattr(bridge, "autostart_enabled", lambda: False)
    monkeypatch.setattr(bridge, "set_autostart", lambda enabled, log: calls.append(enabled))

    result = bridge.ensure_autostart_default(log, state_path)

    assert result is False
    assert calls == [], "set_autostart() haette bei bewusst deaktivierter Praeferenz nicht aufgerufen werden duerfen"


def test_already_enabled_does_not_call_set_autostart_again(tmp_path, monkeypatch):
    """Praeferenz an, macOS-Zustand bereits an => kein unnoetiger osascript-Aufruf."""
    state_path = tmp_path / "state.json"
    state_path.write_text('{"autostart_preference": true}')
    calls = []
    monkeypatch.setattr(bridge, "autostart_enabled", lambda: True)
    monkeypatch.setattr(bridge, "set_autostart", lambda enabled, log: calls.append(enabled))

    result = bridge.ensure_autostart_default(log, state_path)

    assert result is True
    assert calls == []


def test_save_autostart_preference_merges_without_clobbering_other_keys(tmp_path):
    """Server nutzt dieselbe State-Datei auch fuer Update-Benachrichtigungen -- das
    Speichern der Autostart-Praeferenz darf andere Keys nicht loeschen."""
    state_path = tmp_path / "state.json"
    state_path.write_text('{"notified_version": "3.0.25"}')

    bridge.save_autostart_preference(state_path, False, log)

    import json
    saved = json.loads(state_path.read_text())
    assert saved == {"notified_version": "3.0.25", "autostart_preference": False}


def test_save_autostart_preference_creates_file_if_missing(tmp_path):
    state_path = tmp_path / "subdir" / "state.json"

    bridge.save_autostart_preference(state_path, True, log)

    import json
    assert json.loads(state_path.read_text()) == {"autostart_preference": True}
