"""Archivio macOS Menubar-App."""
from __future__ import annotations

import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
import rumps
import yaml

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def _api_base() -> str:
    try:
        cfg  = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        host = cfg.get("server", {}).get("host", "127.0.0.1") or "127.0.0.1"
        port = cfg.get("server", {}).get("port", 8000)
        if host == "0.0.0.0":
            host = "127.0.0.1"
        return f"http://{host}:{port}"
    except Exception:
        return "http://127.0.0.1:8000"


# ── Config-Helpers ────────────────────────────────────────────────────────────

def _load_scan_time() -> str:
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text())
        return cfg.get("scheduler", {}).get("scan_time", "02:00")
    except Exception:
        return "02:00"


def _save_scan_time(t: str):
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        cfg.setdefault("scheduler", {})["scan_time"] = t
        CONFIG_PATH.write_text(yaml.dump(cfg, allow_unicode=True, sort_keys=False))
    except Exception:
        pass


# ── App ───────────────────────────────────────────────────────────────────────

_ICON         = str(Path(__file__).parent / "icon.png")
_VERSION_FILE = Path(__file__).parent.parent / "VERSION"


def _server_version() -> str:
    try:
        return _VERSION_FILE.read_text().strip()
    except Exception:
        return "?"


class ArchivioMenubar(rumps.App):
    def __init__(self):
        super().__init__("", icon=_ICON, template=True, quit_button=None)
        self.scan_time = _load_scan_time()

        self._version = rumps.MenuItem(f"Version {_server_version()}")
        self._srv    = rumps.MenuItem("⬤  Webserver …")
        self._nas    = rumps.MenuItem("⬤  NAS …")
        self._db     = rumps.MenuItem("⬤  Datenbank …")
        self._last   = rumps.MenuItem("⬤  Letzter Scan …")
        self._sep1   = rumps.separator
        self._timebtn = rumps.MenuItem(
            f"Scan täglich um {self.scan_time}",
            callback=self.change_scan_time,
        )
        self._sep2   = rumps.separator
        self._scanbtn = rumps.MenuItem("Jetzt scannen",   callback=self.scan_now)
        self._openbtn = rumps.MenuItem("Archivio öffnen", callback=self.open_browser)
        self._sep3   = rumps.separator
        self._quitbtn = rumps.MenuItem("Beenden",         callback=rumps.quit_application)

        self.menu = [
            self._version,
            self._srv, self._nas, self._db, self._last,
            self._sep1, self._timebtn, self._sep2,
            self._scanbtn, self._openbtn,
            self._sep3, self._quitbtn,
        ]

        threading.Thread(target=self._status_loop,    daemon=True).start()
        threading.Thread(target=self._scheduler_loop, daemon=True).start()

    # ── Status ────────────────────────────────────────────────────────────────

    def _status_loop(self):
        while True:
            self._refresh_status()
            time.sleep(30)

    def _refresh_status(self):
        try:
            data = requests.get(f"{_api_base()}/api/status", timeout=3).json()
        except Exception:
            data = {}

        ok   = data.get("server", False)
        nas  = data.get("nas",    False)
        docs = data.get("doc_count", "—")
        last = (data.get("last_scan") or "")[:10] or "—"

        if last and last != "—":
            try:
                y, m, d = last.split("-")
                last = f"{d}.{m}.{y}"
            except ValueError:
                pass

        self._srv.title  = f"{'🟢' if ok  else '🔴'}  Webserver {'läuft'        if ok  else 'offline'}"
        self._nas.title  = f"{'🟢' if nas else '🔴'}  NAS       {'verbunden'     if nas else 'nicht verbunden'}"
        self._db.title   = f"🟢  Datenbank    {docs} Dokumente" if ok else "⚫  Datenbank"
        self._last.title = f"🕐  Letzter Scan  {last}"

    # ── Scheduler ─────────────────────────────────────────────────────────────

    def _scheduler_loop(self):
        triggered_today = None
        while True:
            now       = datetime.now()
            today_key = now.strftime("%Y-%m-%d")
            if now.strftime("%H:%M") == self.scan_time and triggered_today != today_key:
                triggered_today = today_key
                self._do_scan()
            time.sleep(30)

    def _do_scan(self):
        try:
            requests.post(f"{_api_base()}/api/scan/all", timeout=5)
        except Exception:
            pass

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def scan_now(self, _):
        self._do_scan()
        rumps.notification(
            title="Archivio",
            subtitle="Scan gestartet",
            message="Alle aktiven Projekte werden gescannt.",
        )

    def open_browser(self, _):
        subprocess.run(["open", _api_base()])

    def change_scan_time(self, sender):
        win = rumps.Window(
            message="Uhrzeit für automatischen Scan:",
            title="Scan-Zeitplan",
            default_text=self.scan_time,
            ok="Speichern",
            cancel="Abbrechen",
            dimensions=(120, 22),
        )
        resp = win.run()
        if not resp.clicked:
            return
        t = resp.text.strip()
        try:
            h, m = t.split(":")
            assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
        except Exception:
            rumps.alert("Bitte im Format HH:MM eingeben, z. B. 03:00")
            return
        self.scan_time  = t
        sender.title    = f"Scan täglich um {t}"
        _save_scan_time(t)


if __name__ == "__main__":
    ArchivioMenubar().run()
