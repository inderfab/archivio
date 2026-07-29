"""Archivio Helper – macOS Menubar-App für Mitarbeiter-Macs."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from pathlib import Path

import requests
import rumps

import menubar_bridge as bridge

HELPER_PORT = bridge.HELPER_PORT

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


# Throttle-Zustand für Update-Benachrichtigungen — im nutzer-schreibbaren Verzeichnis
# (das App-Bundle selbst ist schreibgeschützt).
STATE_PATH = Path.home() / ".archivio" / "helper_state.json"
_UPDATE_NOTIFY_INTERVAL = 7 * 24 * 3600   # max. 1 Benachrichtigung pro Woche


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict):
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    except Exception as e:
        log.error("State save failed: %s", e)


def _should_notify_update(version: str) -> bool:
    """True wenn eine Update-Benachrichtigung gezeigt werden soll: bei neuer
    Version sofort, sonst höchstens einmal pro Woche."""
    import time
    state = _load_state()
    if state.get("last_notified_version") != version:
        return True
    last = state.get("last_update_notify", 0)
    return (time.time() - last) >= _UPDATE_NOTIFY_INTERVAL


def _mark_update_notified(version: str):
    import time
    state = _load_state()
    state["last_notified_version"] = version
    state["last_update_notify"]    = time.time()
    _save_state(state)


# ── Server-Discovery (mDNS) ───────────────────────────────────────────────────
# Ersetzt das fruehere Vorbelegen der server_url beim Zip-Download (brach dort die
# Codesignatur). Der Helper sucht stattdessen selbst im LAN -- meist gibt es nur
# einen Archivio Server pro Buero.

_DEFAULT_SERVER_URLS = {"http://localhost:8000", "http://127.0.0.1:8000"}


# ── Update ────────────────────────────────────────────────────────────────────

def _check_update() -> tuple[str, str] | None:
    """Fragt den Archivio-Server nach der aktuellen Version."""
    try:
        cfg    = _load_config()
        server = cfg.get("server_url", "http://127.0.0.1:8000").rstrip("/")
        resp   = requests.get(f"{server}/api/version", timeout=5)
        if resp.status_code != 200:
            return None
        data    = resp.json()
        # Gegen die HELPER-Version vergleichen, nicht die Server-Version — sonst
        # loest jedes Server-Update faelschlich einen Helper-Update-Hinweis aus.
        remote  = data.get("helper_version") or data.get("version", "")
        current = _local_version()
        if remote and remote != current:
            download_url = f"{server}/dashboard/download/helper"
            return remote, download_url
        return None
    except Exception as e:
        log.warning("Update check failed: %s", e)
        return None


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
        self._search_item    = rumps.MenuItem(
            "Server suchen", callback=self.search_server)
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
            self._search_item,
            self._autostart_item,
            rumps.separator,
            rumps.MenuItem("Archivio öffnen", callback=self.open_browser),
            rumps.separator,
            rumps.MenuItem("Beenden", callback=rumps.quit_application),
        ]
        # Reparatur EINMALIG beim Start — kaputte Login-Items (frueherer Pfad-Bug,
        # zeigen auf Contents statt die .app, oeffnen bei jedem Login ein
        # Finder-Fenster) entfernen. Ohne diese Reparatur wuerde ein kaputter Eintrag
        # NIE von selbst verschwinden: er startet die App ja nicht (nur Finder),
        # kann also auch keinen eigenen Reparatur-Code ausloesen — erst das manuelle
        # Oeffnen der App (dieser Code-Pfad hier) heilt ihn.
        bridge.repair_broken_autostart_entries("Archivio Helper", log)
        self._autostart_item.state = bridge.autostart_enabled()

        bridge.start_local_server(
            "Archivio Helper", log,
            config_provider=lambda: _load_config().get("server_url", ""),
        )
        bridge.register_url_handler(log)
        bridge.ensure_mcp_registered("Archivio Helper", log)
        bridge.ensure_quick_action_installed(log)
        threading.Thread(target=self._status_loop, daemon=True).start()
        # Update-Check kurz nach dem Start (5s warten bis Server erreichbar)
        threading.Thread(target=self._delayed_update_check, daemon=True).start()
        # Automatische Server-Suche nur wenn server_url noch der Auslieferungs-Default
        # ist -- eine bereits manuell/automatisch gesetzte Adresse wird nie ungefragt
        # ueberschrieben.
        if self._server_url in _DEFAULT_SERVER_URLS:
            threading.Thread(target=self._auto_discover, daemon=True).start()
        log.info("ArchivioHelper ready")

    def _auto_discover(self):
        found = bridge.discover_servers(timeout=4, log=log)
        decision, url = bridge.resolve_discovery(found)
        if decision == "one":
            self._apply_server_url(url)
            rumps.notification("Archivio Helper", "Automatisch verbunden",
                                f"Server gefunden: {url}")
            log.info("Automatisch verbunden: %s", url)
        elif decision == "multiple":
            log.info("Mehrere Server gefunden, keine automatische Auswahl: %s", found)
            rumps.notification(
                "Archivio Helper", "Mehrere Archivio-Server gefunden",
                "Bitte im Menü unter «Server suchen» manuell auswählen.",
            )
        else:
            log.info("Keine Archivio-Server im Netz gefunden")

    def _apply_server_url(self, url: str):
        self._server_url = url
        self._server_item.title = f"Server: {url}"
        cfg = _load_config()
        cfg["server_url"] = url
        _save_config(cfg)

    def search_server(self, _):
        found = bridge.discover_servers(timeout=4, log=log)
        decision, url = bridge.resolve_discovery(found)
        if decision == "none":
            rumps.alert("Kein Archivio Server im Netzwerk gefunden.")
            return
        if decision == "one":
            self._apply_server_url(url)
            rumps.alert(f"Verbunden mit Archivio Server: {self._server_url}")
            return
        # Mehrere gefunden: change_server()-Fenster oeffnen, vorbelegt mit dem ersten
        # Treffer, Alternativen in der Meldung auflisten -- kein natives Dropdown in rumps.
        alternatives = "\n".join(f"http://{h}:{p}" for h, p in found)
        host, port = found[0]
        win = rumps.Window(
            message=f"Mehrere Server gefunden:\n{alternatives}\n\nServer-URL wählen:",
            title="Archivio Server",
            default_text=f"http://{host}:{port}",
            ok="Verbinden",
            cancel="Abbrechen",
            dimensions=(300, 22),
        )
        resp = win.run()
        if not resp.clicked:
            return
        url = resp.text.strip().rstrip("/")
        if not url.startswith("http"):
            rumps.alert("Bitte eine gültige URL eingeben, z. B. http://localhost:8000")
            return
        self._apply_server_url(url)

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
            # Benachrichtigung höchstens 1x/Woche (Menüpunkt bleibt sichtbar gelb)
            if _should_notify_update(version):
                rumps.notification(
                    "Archivio Helper",
                    f"Update verfügbar: Version {version}",
                    "Im Menü auf «Update verfügbar» klicken.",
                )
                _mark_update_notified(version)
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
        self._apply_server_url(url)

    def toggle_autostart(self, sender):
        new_state = sender.state != 1  # 1 = aktiv → deaktivieren, sonst aktivieren
        bridge.set_autostart(new_state, log)
        sender.state = new_state


if __name__ == "__main__":
    try:
        ArchivioHelper().run()
    except Exception as e:
        log.exception("Fatal error: %s", e)
        raise
