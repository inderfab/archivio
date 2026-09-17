"""Tests für /api/update/check -- muss echten Versionsvergleich nutzen (packaging.
Version), nicht Text-Ungleichheit. Sonst haelt ein Server, der bereits neuer ist als
das zuletzt veroeffentlichte GitHub-Release (z.B. frisch gebaut, noch nicht releast),
das aeltere Release faelschlich fuer ein Update und bietet ein Downgrade an."""
import requests


class _FakeResponse:
    def __init__(self, status_code=200, tag_name=""):
        self.status_code = status_code
        self._tag_name = tag_name

    def json(self):
        return {"tag_name": self._tag_name}


def _mock_latest_tag(monkeypatch, tag_name):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(200, tag_name))


def _current_version(tmp_path, monkeypatch, version):
    version_file = tmp_path / "VERSION"
    version_file.write_text(version)
    import web.api as api
    monkeypatch.setattr(api, "_VERSION_FILE", version_file)


def test_update_check_finds_newer_release(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from web.main import app

    _current_version(tmp_path, monkeypatch, "3.2.4")
    _mock_latest_tag(monkeypatch, "v3.2.5")

    r = TestClient(app).get("/api/update/check")
    assert r.status_code == 200
    data = r.json()
    assert data["update_available"] is True
    assert data["latest"] == "3.2.5"


def test_update_check_does_not_offer_downgrade(tmp_path, monkeypatch):
    """Regression: Server ist bereits 3.2.5, das zuletzt veroeffentlichte GitHub-
    Release ist aber noch das aeltere 3.0.40 -- kein Update anbieten."""
    from fastapi.testclient import TestClient
    from web.main import app

    _current_version(tmp_path, monkeypatch, "3.2.5")
    _mock_latest_tag(monkeypatch, "v3.0.40")

    r = TestClient(app).get("/api/update/check")
    assert r.status_code == 200
    assert r.json()["update_available"] is False


def test_update_check_same_version_no_update(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from web.main import app

    _current_version(tmp_path, monkeypatch, "3.2.5")
    _mock_latest_tag(monkeypatch, "v3.2.5")

    r = TestClient(app).get("/api/update/check")
    assert r.json()["update_available"] is False


def test_update_check_falls_back_to_string_compare_on_invalid_version(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from web.main import app

    _current_version(tmp_path, monkeypatch, "3.2.5")
    _mock_latest_tag(monkeypatch, "v-not-a-version")

    r = TestClient(app).get("/api/update/check")
    assert r.status_code == 200
    assert r.json()["update_available"] is True
