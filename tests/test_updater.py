import subprocess
from pathlib import Path

import pytest

from menubar import updater


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self._content = content

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1024):
        yield self._content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _release(version, assets):
    return _FakeResponse(200, {"tag_name": f"v{version}", "assets": assets})


def _asset(name, url="https://example.com/download"):
    return {"name": name, "browser_download_url": url}


# ── pruefe_update ─────────────────────────────────────────────────────────────

def test_pruefe_update_findet_neuere_version(monkeypatch):
    monkeypatch.setattr(
        updater.requests, "get",
        lambda *a, **k: _release("3.0.19", [_asset("archivio-server-3.0.19.pkg")]),
    )
    info = updater.pruefe_update("3.0.18")
    assert info == updater.UpdateInfo(
        version="3.0.19",
        download_url="https://example.com/download",
        asset_name="archivio-server-3.0.19.pkg",
    )


def test_pruefe_update_gleiche_version_gibt_none(monkeypatch):
    monkeypatch.setattr(
        updater.requests, "get",
        lambda *a, **k: _release("3.0.18", [_asset("archivio-server-3.0.18.pkg")]),
    )
    assert updater.pruefe_update("3.0.18") is None


def test_pruefe_update_aeltere_version_gibt_none(monkeypatch):
    monkeypatch.setattr(
        updater.requests, "get",
        lambda *a, **k: _release("3.0.17", [_asset("archivio-server-3.0.17.pkg")]),
    )
    assert updater.pruefe_update("3.0.18") is None


def test_pruefe_update_http_fehler_gibt_none(monkeypatch):
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: _FakeResponse(500))
    assert updater.pruefe_update("3.0.18") is None


def test_pruefe_update_netzwerkfehler_gibt_none(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("kein Netz")
    monkeypatch.setattr(updater.requests, "get", boom)
    assert updater.pruefe_update("3.0.18") is None


def test_pruefe_update_kein_passendes_asset_gibt_none(monkeypatch):
    monkeypatch.setattr(
        updater.requests, "get",
        lambda *a, **k: _release("3.0.19", [_asset("irgendwas-anderes.zip")]),
    )
    assert updater.pruefe_update("3.0.18") is None


# ── lade_und_pruefe / _verify_pkg ──────────────────────────────────────────────

def _fake_completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_lade_und_pruefe_gueltige_signatur_behaelt_datei(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "_DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(
        updater.requests, "get",
        lambda *a, **k: _FakeResponse(200, content=b"pkg-inhalt"),
    )

    def fake_run(cmd, **kwargs):
        if cmd[0] == "pkgutil":
            return _fake_completed(0, stdout="Developer ID Installer: Fabio (2USYCLVGTM)")
        if cmd[0] == "spctl":
            return _fake_completed(0)
        raise AssertionError(f"unerwarteter Aufruf: {cmd}")

    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    info = updater.UpdateInfo(version="3.0.19", download_url="https://x", asset_name="a.pkg")
    result = updater.lade_und_pruefe(info)
    assert result == tmp_path / "a.pkg"
    assert result.exists()


def test_lade_und_pruefe_falsche_team_id_loescht_datei(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "_DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(
        updater.requests, "get",
        lambda *a, **k: _FakeResponse(200, content=b"pkg-inhalt"),
    )

    def fake_run(cmd, **kwargs):
        if cmd[0] == "pkgutil":
            return _fake_completed(0, stdout="Developer ID Installer: Boese (ANDEREID123)")
        if cmd[0] == "spctl":
            return _fake_completed(0)
        raise AssertionError(f"unerwarteter Aufruf: {cmd}")

    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    info = updater.UpdateInfo(version="3.0.19", download_url="https://x", asset_name="a.pkg")
    result = updater.lade_und_pruefe(info)
    assert result is None
    assert not (tmp_path / "a.pkg").exists()


def test_lade_und_pruefe_falscher_signer_typ_loescht_datei(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "_DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(
        updater.requests, "get",
        lambda *a, **k: _FakeResponse(200, content=b"pkg-inhalt"),
    )

    def fake_run(cmd, **kwargs):
        if cmd[0] == "pkgutil":
            return _fake_completed(0, stdout="Developer ID Application: Fabio (2USYCLVGTM)")
        raise AssertionError(f"unerwarteter Aufruf: {cmd}")

    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    info = updater.UpdateInfo(version="3.0.19", download_url="https://x", asset_name="a.pkg")
    result = updater.lade_und_pruefe(info)
    assert result is None
    assert not (tmp_path / "a.pkg").exists()


def test_lade_und_pruefe_spctl_ablehnung_loescht_datei(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "_DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(
        updater.requests, "get",
        lambda *a, **k: _FakeResponse(200, content=b"pkg-inhalt"),
    )

    def fake_run(cmd, **kwargs):
        if cmd[0] == "pkgutil":
            return _fake_completed(0, stdout="Developer ID Installer: Fabio (2USYCLVGTM)")
        if cmd[0] == "spctl":
            return _fake_completed(1, stderr="rejected")
        raise AssertionError(f"unerwarteter Aufruf: {cmd}")

    monkeypatch.setattr(updater.subprocess, "run", fake_run)

    info = updater.UpdateInfo(version="3.0.19", download_url="https://x", asset_name="a.pkg")
    result = updater.lade_und_pruefe(info)
    assert result is None
    assert not (tmp_path / "a.pkg").exists()


def test_lade_und_pruefe_download_fehler_gibt_none(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "_DOWNLOAD_DIR", tmp_path)

    def boom(*a, **k):
        raise ConnectionError("kein Netz")
    monkeypatch.setattr(updater.requests, "get", boom)

    info = updater.UpdateInfo(version="3.0.19", download_url="https://x", asset_name="a.pkg")
    assert updater.lade_und_pruefe(info) is None


# ── installiere ─────────────────────────────────────────────────────────────

def test_installiere_oeffnet_pkg(monkeypatch):
    calls = []
    monkeypatch.setattr(updater.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    updater.installiere(Path("/tmp/a.pkg"))
    assert calls == [["open", "/tmp/a.pkg"]]
