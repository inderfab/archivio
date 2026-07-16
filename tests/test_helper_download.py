"""Test für den dynamisch gepatchten Helper-Download (/dashboard/download/helper) —
server_url wird auf die aktuelle Server-Adresse vorausgefüllt, ohne die Ausführ-Rechte
des Launcher-Skripts in der ZIP zu verlieren (bekannte Falle beim Zip-Neuaufbau)."""
import io
import json
import zipfile

from fastapi.testclient import TestClient

import web.dashboard as dash
from web.main import app


def test_download_helper_patches_server_url_and_keeps_exec_bit(tmp_path, monkeypatch):
    # Minimale Fake-Helper-ZIP mit derselben Struktur wie ein echter Build
    zip_path = tmp_path / "archivio-helper-9.9.9.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        cfg = json.dumps({"server_url": "http://127.0.0.1:8000", "version": "1.0.0"})
        z.writestr("Archivio Helper.app/Contents/Resources/config.json", cfg)
        info = zipfile.ZipInfo("Archivio Helper.app/Contents/MacOS/Archivio Helper")
        info.external_attr = (0o755 << 16)  # rwxr-xr-x, wie der echte Launcher
        z.writestr(info, "#!/bin/bash\necho hi\n")

    version_file = tmp_path / "VERSION"
    version_file.write_text("9.9.9")
    monkeypatch.setattr(dash, "_DIST", tmp_path)
    monkeypatch.setattr(dash, "_DIST_DATA", tmp_path / "does-not-exist")
    monkeypatch.setattr(dash, "_VERSION_FILE", version_file)

    expected_url = dash._helper_url_hint(dash.settings.load_all())

    c = TestClient(app)
    r = c.get("/dashboard/download/helper")
    assert r.status_code == 200

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        cfg_out = json.loads(z.read("Archivio Helper.app/Contents/Resources/config.json"))
        assert cfg_out["server_url"] == expected_url
        assert cfg_out["version"] == "1.0.0"  # andere Felder unangetastet

        info_out = z.getinfo("Archivio Helper.app/Contents/MacOS/Archivio Helper")
        mode = (info_out.external_attr >> 16) & 0o777
        assert mode & 0o100, "Ausführ-Bit des Launchers ging beim Zip-Patch verloren"


def test_download_helper_falls_back_gracefully_on_broken_config_json(tmp_path, monkeypatch):
    """Ein kaputtes/nicht-JSON config.json darf den Download nicht zum Absturz bringen —
    die Datei wird dann einfach unverändert durchgereicht."""
    zip_path = tmp_path / "archivio-helper-9.9.9.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("Archivio Helper.app/Contents/Resources/config.json", "not-json-at-all")

    version_file = tmp_path / "VERSION"
    version_file.write_text("9.9.9")
    monkeypatch.setattr(dash, "_DIST", tmp_path)
    monkeypatch.setattr(dash, "_DIST_DATA", tmp_path / "does-not-exist")
    monkeypatch.setattr(dash, "_VERSION_FILE", version_file)

    c = TestClient(app)
    r = c.get("/dashboard/download/helper")
    assert r.status_code == 200

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert z.read("Archivio Helper.app/Contents/Resources/config.json") == b"not-json-at-all"
