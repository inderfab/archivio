"""Test für den Helper-Download (/dashboard/download/helper) — liefert die ZIP
unveraendert aus. Frueher wurde server_url in config.json innerhalb der bereits
signierten+notarisierten Zip vorausgefuellt (_patch_zip()); das brach die
Codesignatur und fuehrte beim Endnutzer zu "Archivio Helper.app ist beschaedigt".
Die server_url wird jetzt per mDNS-Discovery vom Helper selbst ermittelt
(shared/menubar_bridge.py, helper/archivio_helper.py)."""
import hashlib
import zipfile

from fastapi.testclient import TestClient

import web.dashboard as dash
from web.main import app


def test_download_helper_returns_zip_byte_identical(tmp_path, monkeypatch):
    zip_path = tmp_path / "archivio-helper-9.9.9.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("Archivio Helper.app/Contents/Resources/config.json",
                    '{"server_url": "http://127.0.0.1:8000", "version": "1.0.0"}')
        info = zipfile.ZipInfo("Archivio Helper.app/Contents/MacOS/Archivio Helper")
        info.external_attr = (0o755 << 16)  # rwxr-xr-x, wie der echte Launcher
        z.writestr(info, "#!/bin/bash\necho hi\n")

    version_file = tmp_path / "VERSION"
    version_file.write_text("9.9.9")
    monkeypatch.setattr(dash, "_DIST", tmp_path)
    monkeypatch.setattr(dash, "_DIST_DATA", tmp_path / "does-not-exist")
    monkeypatch.setattr(dash, "_VERSION_FILE", version_file)

    c = TestClient(app)
    r = c.get("/dashboard/download/helper")
    assert r.status_code == 200

    # Byte-identisch mit der Quelldatei -- keine Modifikation, keine gebrochene Signatur.
    assert hashlib.sha256(r.content).hexdigest() == hashlib.sha256(zip_path.read_bytes()).hexdigest()

    with zipfile.ZipFile(__import__("io").BytesIO(r.content)) as z:
        info_out = z.getinfo("Archivio Helper.app/Contents/MacOS/Archivio Helper")
        mode = (info_out.external_attr >> 16) & 0o777
        assert mode & 0o100, "Ausführ-Bit des Launchers darf beim unveraenderten Download nicht verloren gehen"


def test_download_helper_404_wenn_kein_build(tmp_path, monkeypatch):
    version_file = tmp_path / "VERSION"
    version_file.write_text("9.9.9")
    monkeypatch.setattr(dash, "_DIST", tmp_path / "does-not-exist-1")
    monkeypatch.setattr(dash, "_DIST_DATA", tmp_path / "does-not-exist-2")
    monkeypatch.setattr(dash, "_VERSION_FILE", version_file)

    c = TestClient(app)
    r = c.get("/dashboard/download/helper")
    assert r.status_code == 404
