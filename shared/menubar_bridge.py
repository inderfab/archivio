"""Gemeinsamer Code für Archivio Server + Archivio Helper (macOS-Menubar-Apps).

Wird von beiden Build-Skripten unverändert nach Contents/Resources/ kopiert und von
archivio_server.py bzw. archivio_helper.py importiert. Verhindert, dass ein Fix (z. B.
der Autostart-Pfad-Bug oder die Quick-Action-Installation) nur in einer der beiden
Kopien nachgezogen wird — das ist in der Vergangenheit mehrfach passiert.

Da diese Datei physisch in Contents/Resources/ jeder .app landet, lösen
Path(__file__).parent-basierte Zugriffe (Quick Action, MCP-Registrierung) für beide
Apps automatisch korrekt auf die jeweils eigene Bundle-Resources auf.
"""
from __future__ import annotations

import http.server
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HELPER_PORT = 44380

_SERVICES_DIR = Path.home() / "Library" / "Services"
_QUICK_ACTION_NAME = "ArchivioLink.workflow"

CLAUDE_CONFIG_PATH = (
    Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
)


def app_path() -> Path:
    """.app-Bundle-Pfad, abgeleitet vom eingebetteten Python — funktioniert für Server
    und Helper gleichermassen (identische Bundle-Tiefe: .../Contents/Resources/
    archivio-python-<arch>/bin/python3)."""
    return Path(sys.executable).parent.parent.parent.parent.parent


def handle_archivio_url(url_str: str, log) -> None:
    log.info("URL empfangen: %s", url_str)
    try:
        parsed = urlparse(url_str)
        if parsed.scheme != "archivio":
            return
        path = unquote(parse_qs(parsed.query).get("path", [""])[0])
        if not path:
            return
        if parsed.hostname == "open":
            subprocess.run(["open", path], timeout=5)
        elif parsed.hostname == "reveal":
            if Path(path).exists():
                subprocess.run(["open", "-R", path], timeout=5)
    except Exception as e:
        log.error("Fehler bei URL-Verarbeitung: %s", e)


def _html_page(app_name: str, message: str, autoclose: bool) -> bytes:
    close_script = (
        "<script>setTimeout(function(){ window.close(); }, 300);</script>"
        if autoclose
        else ""
    )
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{app_name}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; display:flex; align-items:center;
          justify-content:center; height:100vh; margin:0; background:#f5f5f5; color:#333; }}
  .box {{ text-align:center; }}
  .hint {{ color:#888; font-size:13px; margin-top:8px; }}
</style></head>
<body><div class="box"><div>{message}</div>
<div class="hint">Dieser Tab kann geschlossen werden.</div></div>
{close_script}
</body></html>"""
    return html.encode("utf-8")


def make_local_http_handler(app_name: str, log, config_provider=None):
    """config_provider: optionales Callable[[], str], das server_url für den
    /config-Endpoint liefert. Helper übergibt einen Reader auf sein config.json,
    Server übergibt einen fest verdrahteten "http://127.0.0.1:8000" (dieselbe Adresse,
    die server_app.py an anderen Stellen bereits hardcoded verwendet)."""

    class _LocalHTTPHandler(http.server.BaseHTTPRequestHandler):
        def do_OPTIONS(self):
            self._cors_headers(200)

        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            path = unquote(params.get("path", [""])[0])

            if parsed.path == "/open" and path:
                if "://" in path and not path.startswith("/"):
                    subprocess.run(["open", path], timeout=5)
                    self._html_response(200, "✓ Wird geöffnet …")
                else:
                    p = Path(path)
                    if p.exists():
                        subprocess.run(["open", str(p)], timeout=5)
                        self._html_response(200, "✓ Wird geöffnet …")
                    else:
                        self._not_found(path)
            elif parsed.path == "/reveal" and path:
                p = Path(path)
                if p.exists():
                    subprocess.run(["open", "-R", str(p)], timeout=5)
                    self._html_response(200, "✓ Im Finder angezeigt")
                else:
                    self._not_found(path)
            elif parsed.path == "/ping":
                self._cors_headers(200)
            elif parsed.path == "/config":
                if config_provider is not None:
                    body = json.dumps({"server_url": config_provider()}).encode("utf-8")
                    self._cors_headers(200, body, "application/json")
                else:
                    self._cors_headers(404)
            else:
                self._cors_headers(404)

        def _not_found(self, path):
            log.warning("Datei nicht gefunden: %s", path)
            self._html_response(404, f"⚠ Datei nicht gefunden: {path}", autoclose=False)

        def _html_response(self, code, message, autoclose=True):
            self._cors_headers(code, _html_page(app_name, message, autoclose), "text/html")

        def _cors_headers(self, code, body=None, content_type="text/plain"):
            self.send_response(code)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", content_type)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def log_message(self, *args):
            pass

    return _LocalHTTPHandler


def start_local_server(app_name: str, log, config_provider=None) -> None:
    handler_cls = make_local_http_handler(app_name, log, config_provider)
    try:
        srv = http.server.HTTPServer(("127.0.0.1", HELPER_PORT), handler_cls)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log.info("Lokaler HTTP-Server gestartet auf localhost:%d", HELPER_PORT)
    except OSError as e:
        log.warning(
            "Lokaler HTTP-Server konnte nicht starten (Port %d belegt?): %s", HELPER_PORT, e
        )


def register_url_handler(log) -> None:
    try:
        from Foundation import NSAppleEventManager, NSObject

        kInternetEventClass = 0x4755524C
        kAEGetURL = 0x4755524C
        keyDirectObject = 0x2D2D2D2D

        class _URLHandler(NSObject):
            def handleGetURLEvent_withReplyEvent_(self, event, reply):
                url = str(event.paramDescriptorForKeyword_(keyDirectObject).stringValue())
                handle_archivio_url(url, log)

        handler = _URLHandler.alloc().init()
        register_url_handler._handler = handler  # Referenz halten, sonst GC
        NSAppleEventManager.sharedAppleEventManager().setEventHandler_andSelector_forEventClass_andEventID_(
            handler, "handleGetURLEvent:withReplyEvent:", kInternetEventClass, kAEGetURL
        )
        log.info("URL-Scheme-Handler registriert")
    except Exception as e:
        log.warning("URL-Scheme-Handler nicht verfügbar: %s", e)


def autostart_enabled() -> bool:
    try:
        p = str(app_path())
        r = subprocess.run(
            [
                "osascript",
                "-e",
                f'tell application "System Events" to return (path of every login item) contains "{p}"',
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.stdout.strip().lower() == "true"
    except Exception:
        return False


def repair_broken_autostart_entries(app_name: str, log) -> bool:
    """Entfernt Login-Items, die (durch den frueheren Pfad-Bug) auf .../Contents statt
    auf die .app selbst zeigen. Gibt True zurueck, wenn etwas bereinigt wurde."""
    bundle_name = app_path().name  # z. B. "Archivio Server.app"
    try:
        r = subprocess.run(
            [
                "osascript",
                "-e",
                f'tell application "System Events" to get path of every login item '
                f'whose path contains "{bundle_name}/Contents"',
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        broken = [x.strip() for x in r.stdout.split(",") if x.strip()]
        if not broken:
            return False
        subprocess.run(
            [
                "osascript",
                "-e",
                f'tell application "System Events" to delete '
                f'(every login item whose path contains "{bundle_name}/Contents")',
            ],
            timeout=5,
        )
        log.info("Kaputte Autostart-Login-Items entfernt: %s", broken)
        import rumps

        rumps.notification(
            app_name,
            "Fehlerhaften Autostart-Eintrag entfernt",
            "Bitte bei Bedarf im Menü erneut aktivieren.",
        )
        return True
    except Exception as e:
        log.warning("Autostart-Reparatur fehlgeschlagen: %s", e)
        return False


def set_autostart(enabled: bool, log) -> None:
    p = str(app_path())
    bundle_name = app_path().name
    if enabled:
        script = (
            f'tell application "System Events" to make new login item at end '
            f'with properties {{path:"{p}", hidden:true}}'
        )
    else:
        script = (
            f'tell application "System Events" to delete '
            f'(every login item whose path contains "{bundle_name}")'
        )
    try:
        subprocess.run(["osascript", "-e", script], timeout=5)
    except Exception as e:
        log.error("Autostart umschalten fehlgeschlagen: %s", e)


def ensure_quick_action_installed(log) -> None:
    try:
        src = Path(__file__).parent / _QUICK_ACTION_NAME
        if not src.exists():
            return
        dst = _SERVICES_DIR / _QUICK_ACTION_NAME
        src_wflow = src / "Contents" / "document.wflow"
        dst_wflow = dst / "Contents" / "document.wflow"
        if dst_wflow.exists() and dst_wflow.read_bytes() == src_wflow.read_bytes():
            return
        _SERVICES_DIR.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        subprocess.run(["/System/Library/CoreServices/pbs", "-flush"], timeout=5)
        log.info("Quick Action installiert/aktualisiert")
    except Exception as e:
        log.warning("Quick-Action-Installation fehlgeschlagen: %s", e)


def ensure_mcp_registered(app_name: str, log) -> None:
    if not CLAUDE_CONFIG_PATH.parent.exists():
        return
    try:
        try:
            cfg = json.loads(CLAUDE_CONFIG_PATH.read_text())
        except Exception:
            cfg = {}
        servers = cfg.setdefault("mcpServers", {})
        target = {
            "command": sys.executable,
            "args": [str(Path(__file__).parent / "archivio_mcp.py")],
        }
        if servers.get("archivio") == target:
            return
        servers["archivio"] = target
        CLAUDE_CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        log.info("Archivio als MCP-Server in Claude Desktop registriert: %s", target)
        import rumps

        rumps.notification(
            app_name,
            "Claude Desktop: Archivio verfügbar",
            "Bitte Claude Desktop neu starten, damit das Archivio-Tool aktiv wird.",
        )
    except Exception as e:
        log.warning("MCP-Registrierung in Claude Desktop fehlgeschlagen: %s", e)


def thread_alert(title: str, message: str) -> None:
    msg = message.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    subprocess.run(["osascript", "-e", f'display alert "{title}" message "{msg}"'], timeout=60)
