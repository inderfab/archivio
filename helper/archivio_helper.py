"""Archivio Helper – macOS Menubar-App für Mitarbeiter-Macs."""
from __future__ import annotations

import http.server
import json
import logging
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests
import rumps

HELPER_PORT = 44380  # lokaler HTTP-Port — kein URL-Scheme, kein macOS-Dialog

# ── Logging ───────────────────────────────────────────────────────────────────
_log_dir = Path.home() / "Library" / "Logs"
_log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(_log_dir / "ArchivioHelper.log"),
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)
log.info("Archivio Helper starting (Python %s)", sys.version)


# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH  = Path(__file__).parent / "config.json"
VERSION_PATH = Path(__file__).parent / "VERSION"


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {"server_url": "http://localhost:8000", "version": "1.0.0",
                "github_repo": "inderfab/archivio"}


def _save_config(cfg: dict):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    except Exception as e:
        log.error("Config save failed: %s", e)


def _local_version() -> str:
    try:
        return VERSION_PATH.read_text().strip()
    except Exception:
        return _load_config().get("version", "1.0.0")


# ── Datei öffnen ──────────────────────────────────────────────────────────────

def _open_path(path: str):
    p = Path(path)
    if not p.exists():
        log.warning("File not found: %s", path)
        rumps.notification("Archivio Helper", "Datei nicht gefunden", path)
        return
    log.info("Opening: %s", path)
    subprocess.run(["open", path], timeout=5)


def _handle_archivio_url(url_str: str):
    log.info("URL received: %s", url_str)
    try:
        parsed = urlparse(url_str)
        if parsed.scheme != "archivio":
            return
        if parsed.hostname == "open":
            path = unquote(parse_qs(parsed.query).get("path", [""])[0])
            if path:
                _open_path(path)
        elif parsed.hostname == "reveal":
            path = unquote(parse_qs(parsed.query).get("path", [""])[0])
            if path:
                p = Path(path)
                if p.exists():
                    subprocess.run(["open", "-R", path], timeout=5)
                else:
                    rumps.notification("Archivio Helper", "Datei nicht gefunden", path)
    except Exception as e:
        log.error("URL handling error: %s", e)


# ── Lokaler HTTP-Helper-Server ────────────────────────────────────────────────
# Läuft auf localhost:44380. Browser ruft direkt http://localhost:44380/open?path=...
# auf — kein URL-Scheme, kein macOS-Sicherheitsdialog, kein "App öffnen?" Prompt.

class _LocalHTTPHandler(http.server.BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self._cors_headers(200)

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        path   = unquote(params.get("path", [""])[0])

        if parsed.path == "/open" and path:
            p = Path(path)
            if p.exists():
                subprocess.run(["open", str(p)], timeout=5)
                self._cors_headers(200)
            else:
                log.warning("Datei nicht gefunden: %s", path)
                rumps.notification("Archivio Helper", "Datei nicht gefunden", path)
                self._cors_headers(404)

        elif parsed.path == "/reveal" and path:
            p = Path(path)
            if p.exists():
                subprocess.run(["open", "-R", str(p)], timeout=5)
                self._cors_headers(200)
            else:
                log.warning("Datei nicht gefunden: %s", path)
                rumps.notification("Archivio Helper", "Datei nicht gefunden", path)
                self._cors_headers(404)

        elif parsed.path == "/ping":
            self._cors_headers(200)

        else:
            self._cors_headers(400)

    def _cors_headers(self, code: int):
        self.send_response(code)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok" if code == 200 else b"error")

    def log_message(self, *args):
        pass  # kein HTTP-Log-Spam


def _start_local_server():
    try:
        server = http.server.HTTPServer(("127.0.0.1", HELPER_PORT), _LocalHTTPHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        log.info("Lokaler Helper-Server gestartet auf localhost:%d", HELPER_PORT)
    except OSError as e:
        log.warning("Lokaler Helper-Server konnte nicht starten (Port %d belegt?): %s",
                    HELPER_PORT, e)


# ── URL-Scheme Handler (optional, benötigt pyobjc) ───────────────────────────

def _register_url_handler():
    try:
        from Foundation import NSAppleEventManager, NSObject
        import objc

        kInternetEventClass = 0x4755524C  # 'GURL'
        kAEGetURL           = 0x4755524C
        keyDirectObject     = 0x2D2D2D2D  # '----'

        class _Handler(NSObject):
            def handleGetURLEvent_withReplyEvent_(self, event, reply):
                url = str(event.paramDescriptorForKeyword_(keyDirectObject).stringValue())
                _handle_archivio_url(url)

        handler = _Handler.alloc().init()
        # Keep a reference so the object isn't garbage-collected
        _register_url_handler._handler = handler
        NSAppleEventManager.sharedAppleEventManager() \
            .setEventHandler_andSelector_forEventClass_andEventID_(
                handler,
                "handleGetURLEvent:withReplyEvent:",
                kInternetEventClass,
                kAEGetURL,
            )
        log.info("URL scheme handler registered")
    except Exception as e:
        log.warning("URL scheme handler not available: %s", e)


# ── Update ────────────────────────────────────────────────────────────────────

def _check_update() -> tuple[str, str] | None:
    """Fragt den Archivio-Server nach der aktuellen Version."""
    try:
        cfg    = _load_config()
        server = cfg.get("server_url", "http://127.0.0.1:8000").rstrip("/")
        resp   = requests.get(f"{server}/api/version", timeout=5)
        if resp.status_code != 200:
            return None
        remote  = resp.json().get("version", "")
        current = _local_version()
        if remote and remote != current:
            download_url = f"{server}/dashboard/download/helper"
            return remote, download_url
        return None
    except Exception as e:
        log.warning("Update check failed: %s", e)
        return None


def _thread_alert(title: str, message: str):
    msg = message.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    subprocess.run(["osascript", "-e",
                    f'display alert "{title}" message "{msg}"'],
                   timeout=60)




# ── Autostart ─────────────────────────────────────────────────────────────────

def _autostart_enabled() -> bool:
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to return '
             '(name of every login item) contains "Archivio Helper"'],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip().lower() == "true"
    except Exception:
        return False


def _set_autostart(enabled: bool):
    path = str(Path(sys.executable).parent.parent.parent.parent)
    if enabled:
        script = (f'tell application "System Events" to make new login item '
                  f'at end with properties {{path:"{path}", hidden:true}}')
    else:
        script = ('tell application "System Events" to delete '
                  '(every login item whose name is "Archivio Helper")')
    try:
        subprocess.run(["osascript", "-e", script], timeout=5)
    except Exception as e:
        log.error("Autostart toggle failed: %s", e)


# ── App ───────────────────────────────────────────────────────────────────────

_ICON = str(Path(__file__).parent / "icon.png")


class ArchivioHelper(rumps.App):
    def __init__(self):
        super().__init__("", icon=_ICON, template=True, quit_button=None)
        cfg = _load_config()
        self._server_url     = cfg.get("server_url", "http://localhost:8000")
        self._pending_update: tuple[str, str] | None = None

        self._title_item     = rumps.MenuItem("Archivio Helper")
        self._version_item   = rumps.MenuItem(f"Version {_local_version()}")
        self._status_item    = rumps.MenuItem("⬤  Verbindung …")
        self._update_item    = rumps.MenuItem("Auf Updates prüfen",
                                              callback=self._update_action)
        self._server_item    = rumps.MenuItem(
            f"Server: {self._server_url}", callback=self.change_server)
        self._autostart_item = rumps.MenuItem(
            "Autostart beim Login", callback=self.toggle_autostart)

        self.menu = [
            self._title_item,
            rumps.separator,
            self._version_item,
            self._status_item,
            self._update_item,
            rumps.separator,
            self._server_item,
            self._autostart_item,
            rumps.separator,
            rumps.MenuItem("Archivio öffnen", callback=self.open_browser),
            rumps.separator,
            rumps.MenuItem("Beenden", callback=rumps.quit_application),
        ]
        self._autostart_item.state = _autostart_enabled()

        _start_local_server()
        _register_url_handler()
        threading.Thread(target=self._status_loop, daemon=True).start()
        # Update-Check kurz nach dem Start (5s warten bis Server erreichbar)
        threading.Thread(target=self._delayed_update_check, daemon=True).start()
        log.info("ArchivioHelper ready")

    def _delayed_update_check(self):
        import time
        time.sleep(5)
        self._silent_update_check()

    def _silent_update_check(self):
        result = _check_update()
        if result:
            version, url = result
            self._pending_update = (version, url)
            self._update_item.title = f"🟡  Update: v{version} verfügbar"
            rumps.notification(
                "Archivio Helper",
                f"Update verfügbar: Version {version}",
                "Im Menü auf «Update verfügbar» klicken.",
            )
            log.info("Update verfügbar: %s", version)
        else:
            self._pending_update = None
            self._update_item.title = "Auf Updates prüfen"

    def _update_action(self, _):
        cfg = _load_config()
        server = cfg.get("server_url", "http://localhost:8000").rstrip("/")
        settings_url = f"{server}/dashboard/settings"
        if self._pending_update:
            version, _ = self._pending_update
            if rumps.alert(
                title="Update verfügbar",
                message=f"Version {version} verfügbar. Zur Download-Seite öffnen?",
                ok="Zur Download-Seite", cancel="Abbrechen",
            ):
                subprocess.run(["open", settings_url])
        else:
            result = _check_update()
            if result is None:
                rumps.alert(f"Archivio Helper {_local_version()} ist aktuell.")
            else:
                version, _ = result
                self._pending_update = (version, _)
                self._update_item.title = f"🟡  Update: v{version} verfügbar"
                if rumps.alert(
                    title="Update verfügbar",
                    message=f"Version {version} verfügbar. Zur Download-Seite öffnen?",
                    ok="Zur Download-Seite", cancel="Abbrechen",
                ):
                    subprocess.run(["open", settings_url])

    def _status_loop(self):
        import time
        tick = 0
        while True:
            self._refresh_status()
            tick += 1
            # Update-Check alle 30 Minuten
            if tick % 60 == 0:
                self._silent_update_check()
            time.sleep(30)

    def _refresh_status(self):
        try:
            ok = requests.get(f"{self._server_url}/api/status", timeout=3).status_code == 200
        except Exception:
            ok = False
        self._status_item.title = (
            f"{'🟢' if ok else '🔴'}  Archivio Server "
            f"{'erreichbar' if ok else 'nicht erreichbar'}"
        )
        # Kein Titeltext — Icon genügt

    def open_browser(self, _):
        subprocess.run(["open", self._server_url])

    def change_server(self, _):
        win = rumps.Window(
            message="Server-URL:",
            title="Archivio Server",
            default_text=self._server_url,
            ok="Speichern",
            cancel="Abbrechen",
            dimensions=(260, 22),
        )
        resp = win.run()
        if not resp.clicked:
            return
        url = resp.text.strip().rstrip("/")
        if not url.startswith("http"):
            rumps.alert("Bitte eine gültige URL eingeben, z. B. http://localhost:8000")
            return
        self._server_url = url
        self._server_item.title = f"Server: {url}"
        cfg = _load_config()
        cfg["server_url"] = url
        _save_config(cfg)

    def toggle_autostart(self, sender):
        new_state = sender.state != 1  # 1 = aktiv → deaktivieren, sonst aktivieren
        _set_autostart(new_state)
        sender.state = new_state


if __name__ == "__main__":
    try:
        ArchivioHelper().run()
    except Exception as e:
        log.exception("Fatal error: %s", e)
        raise
