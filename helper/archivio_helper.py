"""Archivio Helper – macOS Menubar-App für Mitarbeiter-Macs.

Registriert das archivio:// URL-Schema und öffnet Dateien direkt vom Browser.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

import objc
import requests
import rumps
from AppKit import NSApplication
from Foundation import NSObject, NSURL

CONFIG_PATH  = Path(__file__).parent / "config.json"
VERSION_PATH = Path(__file__).parent.parent / "VERSION"


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {"server_url": "http://imac.local:8000", "version": "1.0.0",
                "github_repo": "inderfab/archivio"}


def _save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def _local_version() -> str:
    try:
        return VERSION_PATH.read_text().strip()
    except Exception:
        return _load_config().get("version", "1.0.0")


# ── URL-Scheme Handler ────────────────────────────────────────────────────────

class _URLHandler(NSObject):
    """Empfängt archivio:// Apple Events."""

    def handleGetURLEvent_withReplyEvent_(self, event, reply):
        url_str = str(event.paramDescriptorForKeyword_(
            objc.selector(None, selector=b'----').selector
        ).stringValue() or "")
        _handle_archivio_url(url_str)


def _handle_archivio_url(url_str: str):
    """Parst archivio://open?path=... und öffnet die Datei."""
    try:
        parsed = urlparse(url_str)
        if parsed.scheme != "archivio":
            return
        if parsed.hostname == "open":
            params = parse_qs(parsed.query)
            path   = unquote(params.get("path", [""])[0])
            if path:
                _open_path(path)
    except Exception as exc:
        rumps.notification("Archivio Helper", "Fehler beim Öffnen", str(exc))


def _open_path(path: str):
    p = Path(path)
    if not p.exists():
        rumps.notification("Archivio Helper", "Datei nicht gefunden", path)
        return
    subprocess.run(["open", path], timeout=5)


# ── Update check ──────────────────────────────────────────────────────────────

def _check_update() -> tuple[str, str] | None:
    """Gibt (neue_version, download_url) zurück oder None."""
    try:
        cfg    = _load_config()
        repo   = cfg.get("github_repo", "inderfab/archivio")
        resp   = requests.get(
            f"https://api.github.com/repos/{repo}/releases/latest",
            timeout=8,
            headers={"Accept": "application/vnd.github+json"},
        )
        if resp.status_code != 200:
            return None
        data    = resp.json()
        remote  = data.get("tag_name", "").lstrip("v")
        current = _local_version()
        if remote and remote != current:
            for asset in data.get("assets", []):
                if asset["name"].endswith(".zip"):
                    return remote, asset["browser_download_url"]
        return None
    except Exception:
        return None


def _do_update(version: str, url: str):
    try:
        resp = requests.get(url, timeout=60, stream=True)
        zip_path = Path("/tmp/archivio-helper-update.zip")
        with open(zip_path, "wb") as f:
            for chunk in resp.iter_content(65536):
                f.write(chunk)
        app_path = Path(sys.executable).parent.parent.parent.parent  # .../Archivio Helper.app
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(app_path.parent)
        zip_path.unlink(missing_ok=True)
        rumps.notification("Archivio Helper", "Update installiert",
                           f"Version {version} — bitte App neu starten.")
    except Exception as exc:
        rumps.notification("Archivio Helper", "Update fehlgeschlagen", str(exc))


# ── Autostart ─────────────────────────────────────────────────────────────────

def _app_path() -> str:
    return str(Path(sys.executable).parent.parent.parent.parent)


def _autostart_is_enabled() -> bool:
    script = (
        'tell application "System Events" to return '
        '(name of every login item) contains "Archivio Helper"'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip().lower() == "true"
    except Exception:
        return False


def _set_autostart(enabled: bool):
    path = _app_path()
    if enabled:
        script = (
            f'tell application "System Events" to make new login item '
            f'at end with properties {{path:"{path}", hidden:true}}'
        )
    else:
        script = (
            'tell application "System Events" to delete '
            '(every login item whose name is "Archivio Helper")'
        )
    try:
        subprocess.run(["osascript", "-e", script], timeout=5)
    except Exception:
        pass


# ── App ───────────────────────────────────────────────────────────────────────

class ArchivioHelper(rumps.App):
    def __init__(self):
        super().__init__("Archivio", quit_button=None)
        cfg = _load_config()
        self._server_url = cfg.get("server_url", "http://imac.local:8000")

        self._status_item = rumps.MenuItem("⬤  Verbindung …")
        self._server_item = rumps.MenuItem(
            f"Server: {self._server_url}", callback=self.change_server
        )
        self._autostart_item = rumps.MenuItem(
            "Autostart beim Login", callback=self.toggle_autostart
        )
        self._autostart_item.state = _autostart_is_enabled()

        self._open_btn   = rumps.MenuItem("Archivio öffnen", callback=self.open_browser)
        self._update_btn = rumps.MenuItem("Auf Updates prüfen", callback=self.check_update)
        self._quit_btn   = rumps.MenuItem("Beenden", callback=rumps.quit_application)

        self.menu = [
            self._status_item,
            rumps.separator,
            self._server_item,
            self._autostart_item,
            rumps.separator,
            self._open_btn,
            self._update_btn,
            rumps.separator,
            self._quit_btn,
        ]

        self._register_url_handler()
        threading.Thread(target=self._status_loop, daemon=True).start()

    # ── URL-Handler registrieren ──────────────────────────────────────────────

    def _register_url_handler(self):
        try:
            from AppKit import NSAppleEventManager
            from Foundation import NSAppleEventDescriptor
            self._url_handler = _URLHandler.alloc().init()
            mgr = NSAppleEventManager.sharedAppleEventManager()
            mgr.setEventHandler_andSelector_forEventClass_andEventID_(
                self._url_handler,
                objc.selector(
                    _URLHandler.handleGetURLEvent_withReplyEvent_,
                    signature=b"v@:@@",
                ),
                0x4755524C,  # kInternetEventClass / 'GURL'
                0x4755524C,  # kAEGetURL
            )
        except Exception:
            pass

    # ── Status ────────────────────────────────────────────────────────────────

    def _status_loop(self):
        while True:
            self._refresh_status()
            import time; time.sleep(30)

    def _refresh_status(self):
        try:
            resp = requests.get(f"{self._server_url}/api/status", timeout=3)
            ok   = resp.status_code == 200
        except Exception:
            ok = False
        self._status_item.title = (
            f"{'🟢' if ok else '🔴'}  Archivio Server "
            f"{'erreichbar' if ok else 'nicht erreichbar'}"
        )

    # ── Callbacks ─────────────────────────────────────────────────────────────

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
            rumps.alert("Bitte eine gültige URL eingeben, z. B. http://imac.local:8000")
            return
        self._server_url = url
        self._server_item.title = f"Server: {url}"
        cfg = _load_config()
        cfg["server_url"] = url
        _save_config(cfg)

    def toggle_autostart(self, sender):
        new_state = not sender.state
        _set_autostart(new_state)
        sender.state = new_state

    def check_update(self, _):
        result = _check_update()
        if result is None:
            rumps.alert(f"Archivio Helper {_local_version()} ist aktuell.")
            return
        version, url = result
        resp = rumps.alert(
            title="Update verfügbar",
            message=f"Version {version} ist verfügbar. Jetzt installieren?",
            ok="Installieren",
            cancel="Abbrechen",
        )
        if resp:
            threading.Thread(target=_do_update, args=(version, url), daemon=True).start()


if __name__ == "__main__":
    ArchivioHelper().run()
