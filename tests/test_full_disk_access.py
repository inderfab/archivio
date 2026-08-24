"""Tests für die Full-Disk-Access-Erkennung (shared/menubar_bridge.py) -- Standard-
technik: TCC.db selbst ist ohne Vollzugriff auf Festplatte nicht lesbar. Apple bietet
keine API um FDA programmatisch zu erteilen, deshalb nur Erkennung + Anleitung."""
from pathlib import Path

from shared import menubar_bridge as bridge


def test_has_full_disk_access_false_when_probe_unreadable(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist" / "TCC.db"
    monkeypatch.setattr(bridge, "_TCC_PROBE", missing)
    assert bridge.has_full_disk_access() is False


def test_has_full_disk_access_true_when_probe_readable(tmp_path, monkeypatch):
    probe = tmp_path / "TCC.db"
    probe.write_bytes(b"x")
    monkeypatch.setattr(bridge, "_TCC_PROBE", probe)
    assert bridge.has_full_disk_access() is True


def test_open_full_disk_access_settings_calls_open(monkeypatch):
    calls = []
    monkeypatch.setattr(bridge.subprocess, "run", lambda args, **kw: calls.append(args))
    bridge.open_full_disk_access_settings()
    assert calls
    assert calls[0][0] == "open"
    assert "Privacy_AllFiles" in calls[0][1]


def test_show_full_disk_access_prompt_opens_settings_only_on_button_click(monkeypatch):
    calls = []

    class _Result:
        stdout = "button returned:Systemeinstellungen öffnen"

    def _fake_run(args, **kw):
        calls.append(args)
        return _Result()

    monkeypatch.setattr(bridge.subprocess, "run", _fake_run)
    bridge.show_full_disk_access_prompt("Archivio Server")
    # osascript-Alert + anschliessendes "open" fuer die Settings
    assert len(calls) == 2
    assert calls[1][0] == "open"


def test_show_full_disk_access_prompt_skips_settings_when_dismissed(monkeypatch):
    calls = []

    class _Result:
        stdout = "button returned:Später"

    def _fake_run(args, **kw):
        calls.append(args)
        return _Result()

    monkeypatch.setattr(bridge.subprocess, "run", _fake_run)
    bridge.show_full_disk_access_prompt("Archivio Server")
    assert len(calls) == 1
