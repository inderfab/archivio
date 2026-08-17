"""Tests für den /copy-to-folder-Endpunkt in shared/menubar_bridge.py -- "Auswahl in
neuen Ordner speichern" im Foto-Browser. Öffnet einen nativen Finder-Ordner-Picker
(osascript) und kopiert die übergebenen Dateien dorthin."""
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


def _start_test_server():
    handler_cls = bridge.make_local_http_handler("Test", log)
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_port
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)
    return srv, port


def _fake_choose_folder(dest_dir: Path):
    """Simuliert osascript "choose folder": returncode 0, stdout = gewählter Pfad."""
    def _run(args, **kwargs):
        return type("R", (), {"returncode": 0, "stdout": str(dest_dir) + "\n", "stderr": ""})()
    return _run


def _fake_cancel():
    def _run(args, **kwargs):
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "User canceled"})()
    return _run


def test_copy_to_folder_copies_files(monkeypatch, tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    f1 = src_dir / "a.jpg"
    f2 = src_dir / "b.jpg"
    f1.write_text("aaa")
    f2.write_text("bbb")

    monkeypatch.setattr(bridge.subprocess, "run", _fake_choose_folder(dest_dir))
    srv, port = _start_test_server()
    try:
        r = requests.post(f"http://127.0.0.1:{port}/copy-to-folder",
                           json={"paths": [str(f1), str(f2)]})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["copied"] == 2
        assert data["errors"] == []
        assert (dest_dir / "a.jpg").read_text() == "aaa"
        assert (dest_dir / "b.jpg").read_text() == "bbb"
    finally:
        srv.shutdown()


def test_copy_to_folder_handles_name_collision(monkeypatch, tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    (dest_dir / "a.jpg").write_text("existing")
    f1 = src_dir / "a.jpg"
    f1.write_text("new-content")

    monkeypatch.setattr(bridge.subprocess, "run", _fake_choose_folder(dest_dir))
    srv, port = _start_test_server()
    try:
        r = requests.post(f"http://127.0.0.1:{port}/copy-to-folder", json={"paths": [str(f1)]})
        data = r.json()
        assert data["copied"] == 1
        assert (dest_dir / "a.jpg").read_text() == "existing"       # unangetastet
        assert (dest_dir / "a (2).jpg").read_text() == "new-content"  # umbenannt kopiert
    finally:
        srv.shutdown()


def test_copy_to_folder_reports_missing_source_file(monkeypatch, tmp_path):
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    monkeypatch.setattr(bridge.subprocess, "run", _fake_choose_folder(dest_dir))
    srv, port = _start_test_server()
    try:
        r = requests.post(f"http://127.0.0.1:{port}/copy-to-folder",
                           json={"paths": [str(tmp_path / "fehlt.jpg")]})
        data = r.json()
        assert data["ok"] is True
        assert data["copied"] == 0
        assert len(data["errors"]) == 1
    finally:
        srv.shutdown()


def test_copy_to_folder_cancelled_dialog(monkeypatch, tmp_path):
    f1 = tmp_path / "a.jpg"
    f1.write_text("x")
    monkeypatch.setattr(bridge.subprocess, "run", _fake_cancel())
    srv, port = _start_test_server()
    try:
        r = requests.post(f"http://127.0.0.1:{port}/copy-to-folder", json={"paths": [str(f1)]})
        data = r.json()
        assert data["ok"] is False
        assert data["cancelled"] is True
    finally:
        srv.shutdown()


def test_copy_to_folder_no_paths_400(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge.subprocess, "run", _fake_choose_folder(tmp_path))
    srv, port = _start_test_server()
    try:
        r = requests.post(f"http://127.0.0.1:{port}/copy-to-folder", json={"paths": []})
        assert r.status_code == 400
    finally:
        srv.shutdown()


def test_options_preflight_allows_post_json(monkeypatch):
    srv, port = _start_test_server()
    try:
        r = requests.options(f"http://127.0.0.1:{port}/copy-to-folder")
        assert r.status_code == 200
        assert "POST" in r.headers.get("Access-Control-Allow-Methods", "")
        assert "content-type" in r.headers.get("Access-Control-Allow-Headers", "").lower()
    finally:
        srv.shutdown()
