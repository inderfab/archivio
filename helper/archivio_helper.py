"""Archivio Helper – macOS Menubar-App für Mitarbeiter-Macs."""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
import subprocess
import sys
import threading
from pathlib import Path

import requests
import rumps

import menubar_bridge as bridge

HELPER_PORT = bridge.HELPER_PORT

# ── Logging ───────────────────────────────────────────────────────────────────
# Rotierend und auf INFO -- vorher DEBUG ohne Groessenbegrenzung. urllib3 schreibt
# pro requests-Aufruf zwei Zeilen, und die Statusschleife fragt alle 30s den Server
# ab; real gemessen waren das 24 MB in dieser Datei, unbegrenzt weiterwachsend.
_log_dir = Path.home() / "Library" / "Logs"
_log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.handlers.RotatingFileHandler(
        str(_log_dir / "ArchivioHelper.log"), maxBytes=5 * 1024 * 1024, backupCount=3,
        encoding="utf-8")],
)
for _noisy in ("urllib3", "zeroconf", "requests"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger(__name__)
log.info("Archivio Helper starting (Python %s)", sys.version)


# ── Config ────────────────────────────────────────────────────────────────────
# WICHTIG: nutzer-schreibbares Verzeichnis, NICHT Path(__file__).parent -- das
# App-Bundle unter /Applications gehört root und ist für den laufenden (nicht-root)
# Prozess schreibgeschützt. Ein CONFIG_PATH im Bundle sähe lesend unauffällig aus
# (der zuletzt bekannte/gebündelte Default wird brav zurückgegeben), aber JEDER
# Schreibversuch (server_url nach "Server suchen"/"Server ändern") schlägt still
# fehl (siehe _save_config()) -- der Helper zeigt im Menü dann zwar die frisch
# gefundene Adresse (self._server_url, nur im Prozessspeicher), aber sowohl ein
# Neustart als auch jeder externe Leser von config.json (z.B. archivio_mcp.py über
# /config) sehen weiterhin den alten, nie tatsächlich gespeicherten Wert. Genau das
# hat vor dieser Änderung dazu geführt, dass MCP nach einem Neustart des Helpers
# wieder auf "localhost:8000" zurückfiel, obwohl das Menü die richtige Adresse
# zeigte. STATE_PATH (unten) macht es fürs Autostart-Flag schon richtig -- gleiches
# Verzeichnis, aus demselben Grund.
_CONFIG_DIR  = Path.home() / ".archivio"
CONFIG_PATH  = _CONFIG_DIR / "helper_config.json"
_BUNDLED_CONFIG_PATH = Path(__file__).parent / "config.json"  # nur als Erstbefüllung
VERSION_PATH = Path(__file__).parent / "VERSION"


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        pass
    # Erstmaliger Start (oder Umstieg von einer Version, die noch ins Bundle
    # schrieb): einmalig aus dem gebündelten Default lesen, falls vorhanden.
    try:
        return json.loads(_BUNDLED_CONFIG_PATH.read_text())
    except Exception:
        return {"server_url": "http://localhost:8000", "version": "1.0.0",
                "github_repo": "inderfab/archivio"}


def _save_config(cfg: dict):
    """Schreibt über eine Temp-Datei und benennt dann um.

    Seit der automatischen Neusuche (_rediscover) kann diese Funktion aus einem
    Hintergrund-Thread kommen, während das Menü gerade denselben Datensatz
    schreibt. Ein direktes write_text() konnte dabei eine halb geschriebene Datei
    hinterlassen oder Schlüssel wie link_action verlieren."""
    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        tmp.replace(CONFIG_PATH)
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

# Nach wie vielen erfolglosen Statusabfragen in Folge (alle 30s) neu gesucht wird.
# 6 = drei Minuten -- lang genug, dass ein Serverneustart oder ein kurzer
# Netzaussetzer keinen Adresswechsel auslöst, kurz genug für einen Umzug.
_REDISCOVER_AFTER_FAILS = 6


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
        # Zähler für die automatische Neusuche (siehe _rediscover)
        self._fail_count     = 0
        self._discovering    = False

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
        self._link_action_item = rumps.MenuItem(
            self._link_action_title(), callback=self.toggle_link_action)
        self._mcp_item = rumps.MenuItem(
            self._mcp_item_title(), callback=self.open_mcp_page)

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
            self._link_action_item,
            self._mcp_item,
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
        self._autostart_item.state = bridge.ensure_autostart_default(log, STATE_PATH)

        bridge.start_local_server(
            "Archivio Helper", log,
            config_provider=lambda: _load_config().get("server_url", ""),
            link_action_provider=lambda: _load_config().get("link_action", "reveal"),
        )
        bridge.register_url_handler(log)
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
        if self._pending_update:
            version, download_url = self._pending_update
        else:
            result = _check_update()
            if result is None:
                rumps.alert(f"Archivio Helper {_local_version()} ist aktuell.")
                return
            version, download_url = result
            self._pending_update = (version, download_url)
            self._update_item.title = f"🟡  Update: v{version} verfügbar"

        if rumps.alert(
            title="Update verfügbar",
            message=f"Version {version} verfügbar. Jetzt herunterladen und installieren?",
            ok="Herunterladen", cancel="Abbrechen",
        ):
            threading.Thread(
                target=self._download_and_install_update, args=(download_url, version), daemon=True
            ).start()

    def _download_and_install_update(self, download_url: str, version: str):
        """Lädt das Helper-.pkg direkt herunter -- OHNE Browser -- und öffnet es. Das
        startet den macOS-Installer, der die laufende Helper-App automatisch beendet,
        ersetzt und neu startet (siehe helper/build.sh), genau wie ein manueller
        Doppelklick auf ein heruntergeladenes .pkg."""
        try:
            resp = requests.get(download_url, timeout=120)
            resp.raise_for_status()
            cd = resp.headers.get("content-disposition", "")
            m = re.search(r'filename="?([^"]+)"?', cd)
            fname = m.group(1) if m else f"archivio-helper-{version}.pkg"
            dest = Path.home() / "Downloads" / fname
            dest.write_bytes(resp.content)
            subprocess.run(["open", str(dest)])
        except Exception as e:
            log.error("Update-Download fehlgeschlagen: %s", e)
            rumps.notification("Archivio Helper", "Update fehlgeschlagen",
                                f"Download nicht möglich: {e}")

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
        self._mcp_item.title = self._mcp_item_title()

        if ok:
            self._fail_count = 0
        else:
            self._fail_count += 1
            # Modulo statt Gleichheit: ist der neue Server beim ersten Versuch noch
            # nicht erreichbar (zieht ja gerade um), wird alle drei Minuten erneut
            # gesucht statt nie wieder.
            if self._fail_count % _REDISCOVER_AFTER_FAILS == 0 and not self._discovering:
                self._discovering = True
                threading.Thread(target=self._rediscover, daemon=True).start()

    def _rediscover(self):
        """Sucht den Server neu, wenn die gespeicherte Adresse dauerhaft tot ist.

        Genau der Umzugsfall: zieht der Server auf einen anderen Mac, zeigen alle
        Arbeitsplätze weiter auf die alte Adresse. Die automatische Suche beim Start
        greift dort nicht, weil sie nur läuft, solange die Adresse noch der
        Auslieferungszustand ist (_DEFAULT_SERVER_URLS) -- eine einmal gesetzte
        Adresse wird bewusst nie ungefragt überschrieben.

        Deshalb hier eng gefasst: erst nach mehreren Fehlschlägen in Folge (nicht bei
        einem kurzen Aussetzer, etwa während der Server selbst neu startet), nur wenn
        GENAU EIN Server gefunden wird, und nur wenn dieser auch wirklich antwortet --
        mDNS-Einträge können veraltet sein und einen längst abgeschalteten Server
        melden.
        """
        try:
            found = bridge.discover_servers(timeout=4, log=log)
            url = bridge.pick_rediscovered_server(
                found, self._server_url,
                lambda u: requests.get(f"{u}/api/status", timeout=3).status_code == 200)
            if not url:
                log.info("Neusuche ohne verwendbares Ergebnis (%d Treffer)", len(found))
                return

            alt = self._server_url
            self._apply_server_url(url)
            self._fail_count = 0
            log.info("Server-Adresse automatisch gewechselt: %s -> %s", alt, url)
            rumps.notification("Archivio Helper", "Server hat gewechselt",
                               f"Jetzt verbunden mit {url}")
        except Exception as exc:
            log.warning("Neusuche fehlgeschlagen: %s", exc)
        finally:
            self._discovering = False

    def _mcp_item_title(self) -> str:
        return ("✓ MCP-Schnittstelle eingerichtet" if bridge.is_mcp_installed()
                else "MCP-Schnittstelle installieren…")

    def open_mcp_page(self, _):
        """Öffnet die MCP-Seite im Browser -- die eigentliche Installation läuft von
        dort aus über /install-mcp auf diesem Helper, nicht mehr automatisch beim
        Start (siehe shared/menubar_bridge.py::install_mcp_client)."""
        subprocess.run(["open", f"{self._server_url}/mcp-log"])

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
        bridge.save_autostart_preference(STATE_PATH, new_state, log)
        sender.state = new_state

    def _link_action_title(self) -> str:
        action = _load_config().get("link_action", "reveal")
        return ("Archivio-Links: Direkt öffnen" if action == "open"
                else "Archivio-Links: Zum Pfad gehen")

    def toggle_link_action(self, sender):
        """Wechselt das Standardverhalten fuer per Quick Action kopierte Links (Landing
        Page unter /link, siehe shared/menubar_bridge.py _link_landing_page) zwischen
        Direkt-Oeffnen und Im-Finder-Zeigen."""
        cfg = _load_config()
        current = cfg.get("link_action", "reveal")
        new_action = "reveal" if current == "open" else "open"
        cfg["link_action"] = new_action
        _save_config(cfg)
        sender.title = self._link_action_title()


if __name__ == "__main__":
    try:
        ArchivioHelper().run()
    except Exception as e:
        log.exception("Fatal error: %s", e)
        raise
