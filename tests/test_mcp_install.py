"""Tests für die manuelle MCP-Installation (shared/menubar_bridge.py) -- ersetzt
die frühere automatische Registrierung beim Helper-/Server-Start. Archivio wird
nur noch auf ausdrücklichen Klick (Button auf der /mcp-log-Seite) als MCP-Server
in einem Client eingetragen, nie mehr stillschweigend beim blossen Öffnen."""
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


def test_is_mcp_installed_false_when_app_missing(monkeypatch, tmp_path):
    monkeypatch.setitem(bridge.MCP_CLIENTS, "claude_desktop", {
        "label": "Claude Desktop",
        "app_path": tmp_path / "nonexistent.app",
        "config_path": tmp_path / "claude_desktop_config.json",
    })
    assert bridge.is_mcp_installed("claude_desktop") is False


def test_install_mcp_client_refuses_when_app_not_installed(monkeypatch, tmp_path):
    # WICHTIG: app_path MUSS auf einen garantiert nicht existierenden Pfad zeigen --
    # NIE den echten _CLAUDE_APP_PATH verwenden. Ist Claude Desktop auf der Maschine,
    # die den Testlauf ausführt, tatsächlich installiert (z.B. dem Entwickler-Mac),
    # würde install_mcp_client() sonst echt in dessen reale
    # claude_desktop_config.json schreiben -- mit dem sys.executable/__file__ DIESES
    # Testprozesses, nicht der echten App. Genau das ist einmal passiert (siehe
    # Git-Historie) und hat die reale MCP-Registrierung auf einem Entwickler-Mac
    # kaputtgemacht. Deshalb hier IMMER mit einem Fake-Pfad arbeiten, nie mit der
    # Existenz des echten Pfads verzweigen.
    monkeypatch.setitem(bridge.MCP_CLIENTS, "claude_desktop", {
        "label": "Claude Desktop",
        "app_path": tmp_path / "nonexistent.app",
        "config_path": tmp_path / "claude_desktop_config.json",
    })
    ok, message = bridge.install_mcp_client("claude_desktop", log)
    assert ok is False
    assert "nicht installiert" in message
    assert not (tmp_path / "claude_desktop_config.json").exists()


def test_install_mcp_client_writes_config(monkeypatch, tmp_path):
    fake_app = tmp_path / "Claude.app"
    fake_app.mkdir()
    config_path = tmp_path / "claude_desktop_config.json"
    monkeypatch.setitem(bridge.MCP_CLIENTS, "claude_desktop", {
        "label": "Claude Desktop", "app_path": fake_app, "config_path": config_path,
    })

    assert bridge.is_mcp_installed("claude_desktop") is False
    ok, message = bridge.install_mcp_client("claude_desktop", log)
    assert ok is True
    assert "eingerichtet" in message
    assert bridge.is_mcp_installed("claude_desktop") is True

    cfg = json.loads(config_path.read_text())
    assert cfg["mcpServers"]["archivio"]["command"] == sys.executable


def test_install_mcp_client_preserves_other_servers(monkeypatch, tmp_path):
    """Ein Klick auf 'installieren' darf andere bereits eingetragene MCP-Server in
    derselben Config-Datei nicht überschreiben."""
    fake_app = tmp_path / "Claude.app"
    fake_app.mkdir()
    config_path = tmp_path / "claude_desktop_config.json"
    config_path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    monkeypatch.setitem(bridge.MCP_CLIENTS, "claude_desktop", {
        "label": "Claude Desktop", "app_path": fake_app, "config_path": config_path,
    })

    bridge.install_mcp_client("claude_desktop", log)
    cfg = json.loads(config_path.read_text())
    assert "other" in cfg["mcpServers"]
    assert "archivio" in cfg["mcpServers"]


def test_install_mcp_client_idempotent_second_call(monkeypatch, tmp_path):
    fake_app = tmp_path / "Claude.app"
    fake_app.mkdir()
    config_path = tmp_path / "claude_desktop_config.json"
    monkeypatch.setitem(bridge.MCP_CLIENTS, "claude_desktop", {
        "label": "Claude Desktop", "app_path": fake_app, "config_path": config_path,
    })

    ok1, msg1 = bridge.install_mcp_client("claude_desktop", log)
    ok2, msg2 = bridge.install_mcp_client("claude_desktop", log)
    assert ok1 is True and ok2 is True
    assert "bereits eingerichtet" in msg2


def test_unknown_client_rejected():
    ok, message = bridge.install_mcp_client("chatgpt", log)
    assert ok is False
    assert "Unbekannter Client" in message


def test_mcp_status_endpoint(monkeypatch, tmp_path):
    fake_app = tmp_path / "Claude.app"
    fake_app.mkdir()
    config_path = tmp_path / "claude_desktop_config.json"
    monkeypatch.setitem(bridge.MCP_CLIENTS, "claude_desktop", {
        "label": "Claude Desktop", "app_path": fake_app, "config_path": config_path,
    })

    srv, port = _start_test_server()
    try:
        r = requests.get(f"http://127.0.0.1:{port}/mcp-status", timeout=2)
        assert r.status_code == 200
        assert r.json() == {"clients": {"claude_desktop": False}}

        bridge.install_mcp_client("claude_desktop", log)
        r2 = requests.get(f"http://127.0.0.1:{port}/mcp-status", timeout=2)
        assert r2.json() == {"clients": {"claude_desktop": True}}
    finally:
        srv.shutdown()


def test_restart_app_endpoint_relaunches_and_exits(monkeypatch):
    """/restart-app antwortet zuerst, spawnt dann einen vom Prozess abgelösten
    Relaunch-Befehl und beendet den eigenen Prozess -- os._exit/Popen werden hier
    gemockt, sonst würde der Test-Runner selbst beendet."""
    exit_calls = []
    popen_calls = []
    monkeypatch.setattr(bridge.os, "_exit", lambda code: exit_calls.append(code))
    monkeypatch.setattr(bridge.subprocess, "Popen", lambda *a, **k: popen_calls.append((a, k)))

    srv, port = _start_test_server()
    try:
        r = requests.post(f"http://127.0.0.1:{port}/restart-app", timeout=2)
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        for _ in range(20):
            if exit_calls:
                break
            time.sleep(0.1)
        assert exit_calls == [0]
        assert popen_calls, "sollte einen Relaunch-Befehl gestartet haben"
        shell_cmd = popen_calls[0][0][0][-1]
        assert 'open -a "Test"' in shell_cmd
    finally:
        srv.shutdown()


def test_install_mcp_endpoint(monkeypatch, tmp_path):
    fake_app = tmp_path / "Claude.app"
    fake_app.mkdir()
    config_path = tmp_path / "claude_desktop_config.json"
    monkeypatch.setitem(bridge.MCP_CLIENTS, "claude_desktop", {
        "label": "Claude Desktop", "app_path": fake_app, "config_path": config_path,
    })

    srv, port = _start_test_server()
    try:
        r = requests.post(
            f"http://127.0.0.1:{port}/install-mcp",
            json={"client": "claude_desktop"}, timeout=2,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert config_path.exists()
    finally:
        srv.shutdown()
