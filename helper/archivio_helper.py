"""Archivio Helper – macOS Menubar-App für Mitarbeiter-Macs."""
from __future__ import annotations

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
        return {"server_url": "http://imac.local:8000", "version": "1.0.0",
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
    except Exception as e:
        log.error("URL handling error: %s", e)


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
    try:
        cfg   = _load_config()
        repo  = cfg.get("github_repo", "inderfab/archivio")
        resp  = requests.get(
            f"https://api.github.com/repos/{repo}/releases/latest",
            timeout=8, headers={"Accept": "application/vnd.github+json"},
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
    except Exception as e:
        log.warning("Update check failed: %s", e)
        return None


def _do_update(version: str, url: str):
    try:
        resp = requests.get(url, timeout=60, stream=True)
        zip_path = Path("/tmp/archivio-helper-update.zip")
        with open(zip_path, "wb") as f:
            for chunk in resp.iter_content(65536):
                f.write(chunk)
        app_path = Path(sys.executable).parent.parent.parent.parent
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(app_path.parent)
        zip_path.unlink(missing_ok=True)
        rumps.notification("Archivio Helper", "Update installiert",
                           f"Version {version} — bitte App neu starten.")
        log.info("Updated to %s", version)
    except Exception as e:
        log.error("Update failed: %s", e)
        rumps.notification("Archivio Helper", "Update fehlgeschlagen", str(e))


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
        self._server_url = cfg.get("server_url", "http://imac.local:8000")

        self._status_item    = rumps.MenuItem("⬤  Verbindung …")
        self._server_item    = rumps.MenuItem(
            f"Server: {self._server_url}", callback=self.change_server)
        self._autostart_item = rumps.MenuItem(
            "Autostart beim Login", callback=self.toggle_autostart)
        self._autostart_item.state = _autostart_enabled()

        self.menu = [
            self._status_item,
            rumps.separator,
            self._server_item,
            self._autostart_item,
            rumps.separator,
            rumps.MenuItem("Archivio öffnen",      callback=self.open_browser),
            rumps.MenuItem("Auf Updates prüfen",   callback=self.check_update),
            rumps.separator,
            rumps.MenuItem("Beenden", callback=rumps.quit_application),
        ]

        _register_url_handler()
        threading.Thread(target=self._status_loop, daemon=True).start()
        log.info("ArchivioHelper ready")

    def _status_loop(self):
        import time
        while True:
            self._refresh_status()
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
        self.title = "☁" if ok else "☁"

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
        if rumps.alert(
            title="Update verfügbar",
            message=f"Version {version} verfügbar. Jetzt installieren?",
            ok="Installieren", cancel="Abbrechen",
        ):
            threading.Thread(target=_do_update, args=(version, url), daemon=True).start()


if __name__ == "__main__":
    try:
        ArchivioHelper().run()
    except Exception as e:
        log.exception("Fatal error: %s", e)
        raise
