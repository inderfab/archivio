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


def _do_update(version: str, url: str):
    import shutil
    log.info("Helper-Update starten: %s", version)
    zip_path  = Path("/tmp/archivio-helper-update.zip")
    tmp_dir   = Path("/tmp/archivio-helper-new")

    # ── Download ──────────────────────────────────────────────────────────────
    try:
        resp = requests.get(url, timeout=120, stream=True)
        resp.raise_for_status()
        with open(zip_path, "wb") as f:
            for chunk in resp.iter_content(65536):
                f.write(chunk)
        log.info("ZIP heruntergeladen: %s bytes", zip_path.stat().st_size)
    except Exception as e:
        log.error("Download fehlgeschlagen: %s", e)
        rumps.alert(title="Update fehlgeschlagen", message=f"Download-Fehler:\n{e}")
        return

    # ── Entpacken ─────────────────────────────────────────────────────────────
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp_dir)
        zip_path.unlink(missing_ok=True)
        new_app = tmp_dir / "Archivio Helper.app"
        if not new_app.exists():
            raise FileNotFoundError(f"Archivio Helper.app nicht in ZIP gefunden")
    except Exception as e:
        log.error("Entpacken fehlgeschlagen: %s", e)
        rumps.alert(title="Update fehlgeschlagen", message=f"Entpack-Fehler:\n{e}")
        return

    # ── App ersetzen ──────────────────────────────────────────────────────────
    # sys.executable: .../Archivio Helper.app/Contents/Resources/.venv/bin/python3
    app_path = Path(sys.executable).parent.parent.parent.parent.parent
    log.info("Ersetze %s", app_path)
    try:
        shutil.rmtree(app_path)
        shutil.copytree(str(new_app), str(app_path))
        log.info("App ersetzt (ohne Admin)")
    except PermissionError:
        # /Applications benötigt Admin-Rechte → osascript-Dialog
        log.info("Permission denied, versuche mit Admin-Rechten")
        src = str(new_app).replace("\\", "\\\\").replace('"', '\\"')
        dst = str(app_path).replace("\\", "\\\\").replace('"', '\\"')
        r = subprocess.run(
            ["osascript", "-e",
             f'do shell script "rm -rf \\"{dst}\\" && cp -r \\"{src}\\" \\"{dst}\\"" '
             f'with administrator privileges'],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            log.error("Admin-Kopie fehlgeschlagen: %s", r.stderr)
            rumps.alert(title="Update fehlgeschlagen",
                        message=f"Konnte App nicht ersetzen:\n{r.stderr or 'Abgebrochen'}")
            return
    except Exception as e:
        log.error("App-Ersatz fehlgeschlagen: %s", e)
        rumps.alert(title="Update fehlgeschlagen", message=str(e))
        return

    shutil.rmtree(tmp_dir, ignore_errors=True)
    log.info("Update %s installiert", version)

    # ── Neustart ──────────────────────────────────────────────────────────────
    restart = Path("/tmp/archivio-helper-restart.sh")
    restart.write_text(f'#!/bin/bash\nsleep 2\nopen "{app_path}"\n')
    restart.chmod(0o755)
    subprocess.Popen(["bash", str(restart)])
    rumps.alert(
        title="Update installiert",
        message=f"Version {version} wurde installiert. Der Helper wird neu gestartet.",
    )
    rumps.quit_application()


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
            self._update_item.title = f"🟡  Update: v{version} installieren"
            rumps.notification(
                "Archivio Helper",
                f"Update verfügbar: Version {version}",
                "Im Menü auf «Update installieren» klicken.",
            )
            log.info("Update verfügbar: %s", version)
        else:
            self._pending_update = None
            self._update_item.title = "Auf Updates prüfen"

    def _update_action(self, _):
        if self._pending_update:
            version, url = self._pending_update
            if rumps.alert(
                title="Update verfügbar",
                message=f"Version {version} verfügbar. Jetzt installieren?",
                ok="Installieren", cancel="Abbrechen",
            ):
                threading.Thread(
                    target=_do_update, args=(version, url), daemon=True
                ).start()
        else:
            # Manuell prüfen
            result = _check_update()
            if result is None:
                rumps.alert(f"Archivio Helper {_local_version()} ist aktuell.")
            else:
                version, url = result
                self._pending_update = (version, url)
                self._update_item.title = f"🟡  Update: v{version} installieren"
                if rumps.alert(
                    title="Update verfügbar",
                    message=f"Version {version} verfügbar. Jetzt installieren?",
                    ok="Installieren", cancel="Abbrechen",
                ):
                    threading.Thread(
                        target=_do_update, args=(version, url), daemon=True
                    ).start()

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
