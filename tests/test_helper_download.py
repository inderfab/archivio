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


def test_helper_info_reads_bundled_helper_version_file(tmp_path, monkeypatch):
    """Server und Helper sind unabhängig versioniert -- _helper_info() muss die
    von scripts/build_server_app.sh geschriebene HELPER_VERSION-Datei lesen,
    nicht die Server-VERSION."""
    (tmp_path / "archivio-helper-3.1.11.zip").write_bytes(b"x")
    (tmp_path / "archivio-helper-3.1.9.zip").write_bytes(b"x")
    helper_version_file = tmp_path / "HELPER_VERSION"
    helper_version_file.write_text("3.1.11")

    monkeypatch.setattr(dash, "_DIST", tmp_path)
    monkeypatch.setattr(dash, "_DIST_DATA", tmp_path / "does-not-exist")
    monkeypatch.setattr(dash, "_HELPER_VERSION_FILE", helper_version_file)

    available, version = dash._helper_info()
    assert available is True
    assert version == "3.1.11"


def test_helper_info_fallback_picks_highest_version_not_lexicographic(tmp_path, monkeypatch):
    """Regressionstest: 'archivio-helper-3.1.9.zip' sortiert alphabetisch NACH
    'archivio-helper-3.1.11.zip' (weil '9' > '1'), was den Download-Endpunkt lange
    die falsche, ältere Helper-Version ausliefern liess. Der Fallback (kein exakter
    HELPER_VERSION-Treffer) muss echte Versionsvergleiche nutzen."""
    (tmp_path / "archivio-helper-3.1.9.zip").write_bytes(b"x")
    (tmp_path / "archivio-helper-3.1.11.zip").write_bytes(b"x")
    (tmp_path / "archivio-helper-3.1.2.zip").write_bytes(b"x")

    monkeypatch.setattr(dash, "_DIST", tmp_path)
    monkeypatch.setattr(dash, "_DIST_DATA", tmp_path / "does-not-exist")
    # Keine der vorhandenen Versionen entspricht der "gewünschten" -> Fallback greift.
    monkeypatch.setattr(dash, "_HELPER_VERSION_FILE", tmp_path / "no-such-file")
    monkeypatch.setattr(dash, "_HELPER_VERSION_FILE_DEV", tmp_path / "no-such-file-either")

    available, version = dash._helper_info()
    assert available is True
    assert version == "3.1.11"
