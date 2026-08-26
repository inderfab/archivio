"""Archivio Server – macOS Menubar-App. Startet und verwaltet den FastAPI-Server."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests
import rumps

import menubar_bridge as bridge
import updater

OLLAMA_EMBED_MODEL = "nomic-embed-text"
OLLAMA_LLM_MODEL   = "llama3.2:3b"
HELPER_PORT        = bridge.HELPER_PORT

# ── Pfade ────────────────────────────────────────────────────────────────────

_HERE = Path(__file__).parent
# Im Bundle liegt web/ neben dieser Datei (Contents/Resources/); in Dev eine Ebene höher
_IN_BUNDLE  = (_HERE / "web").exists()
_CODE_ROOT  = _HERE if _IN_BUNDLE else _HERE.parent
_DATA_DIR   = (Path.home() / "Library" / "Application Support" / "Archivio") if _IN_BUNDLE else _CODE_ROOT
_VENV       = _DATA_DIR / ".venv" if _IN_BUNDLE else _CODE_ROOT / ".venv"
_VERSION    = _HERE / "VERSION" if _IN_BUNDLE else _CODE_ROOT / "VERSION"
_EXAMPLE    = _CODE_ROOT / "config.yaml.example"
_UPDATE_STATE = _DATA_DIR / "update_state.json"

# ── Logging ───────────────────────────────────────────────────────────────────

_log_dir = Path.home() / "Library" / "Logs"
_log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(_log_dir / "ArchivioServer.log"),
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)
log.info("Archivio Server starting (Python %s, bundle=%s)", sys.version, _IN_BUNDLE)

# ── Ollama-Verwaltung ─────────────────────────────────────────────────────────

_ollama_proc: subprocess.Popen | None = None
_ollama_lock = threading.Lock()
_OLLAMA_APP  = Path("/Applications/Ollama.app")
_OLLAMA_BIN  = (
    shutil.which("ollama") or
    next((str(p) for p in [
        Path("/opt/homebrew/bin/ollama"),                        # Apple Silicon + Homebrew
        Path("/usr/local/bin/ollama"),                           # Intel + Homebrew
        Path("/Applications/Ollama.app/Contents/MacOS/ollama"), # curl-Install / Ollama.app
    ] if p.exists()), None)
)


def _is_ollama_running() -> bool:
    try:
        requests.get("http://localhost:11434/", timeout=2)
        return True
    except Exception:
        return False


def _ollama_available() -> bool:
    """Gibt True zurück wenn Ollama installiert ist (App oder CLI)."""
    return bool(_OLLAMA_BIN) or _OLLAMA_APP.exists()


def _start_ollama():
    global _ollama_proc
    with _ollama_lock:
        if _is_ollama_running():
            return
        # CLI-Binary bevorzugen (headless); Fallback: Ollama.app öffnen
        bin_is_standalone = (
            _OLLAMA_BIN and
            "/Applications/Ollama.app" not in _OLLAMA_BIN and
            Path(_OLLAMA_BIN).exists()
        )
        try:
            if bin_is_standalone:
                _ollama_proc = subprocess.Popen(
                    [_OLLAMA_BIN, "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                log.info("ollama serve gestartet (pid %s)", _ollama_proc.pid)
            elif _OLLAMA_APP.exists():
                subprocess.Popen(["open", "-a", "Ollama"])
                log.info("Ollama.app gestartet via 'open -a Ollama'")
            else:
                log.warning("Ollama nicht gefunden")
                return
            for _ in range(20):
                time.sleep(0.5)
                if _is_ollama_running():
                    break
        except Exception as e:
            log.error("Ollama start fehlgeschlagen: %s", e)


def _stop_ollama():
    global _ollama_proc
    with _ollama_lock:
        if _ollama_proc:
            try:
                _ollama_proc.terminate()
                _ollama_proc.wait(timeout=5)
            except Exception:
                _ollama_proc.kill()
            _ollama_proc = None


def _pull_model_if_missing(model: str):
    """Lädt Ollama-Modell herunter falls noch nicht vorhanden."""
    try:
        resp   = requests.get("http://localhost:11434/api/tags", timeout=5)
        models = [m["name"].split(":")[0] for m in resp.json().get("models", [])]
        if model.split(":")[0] in models:
            return
        log.info("Ziehe Ollama-Modell: %s", model)
        requests.post(
            "http://localhost:11434/api/pull",
            json={"name": model, "stream": False},
            timeout=600,
        )
        log.info("Modell %s geladen", model)
    except Exception as e:
        log.warning("Modell-Pull %s fehlgeschlagen: %s", model, e)


def _ensure_ollama_models():
    _start_ollama()
    if _is_ollama_running():
        _pull_model_if_missing(OLLAMA_EMBED_MODEL)
        _pull_model_if_missing(OLLAMA_LLM_MODEL)


def _ollama_status_label() -> str:
    if not _ollama_available():
        return "🔴  KI-Suche (Ollama fehlt)"
    if not _is_ollama_running():
        return "🔴  KI-Suche offline"
    try:
        resp   = requests.get("http://localhost:11434/api/tags", timeout=3)
        models = [m["name"].split(":")[0] for m in resp.json().get("models", [])]
        both   = (OLLAMA_EMBED_MODEL.split(":")[0] in models and
                  OLLAMA_LLM_MODEL.split(":")[0] in models)
        return "🟢  KI-Suche bereit" if both else "🟡  KI-Modelle laden…"
    except Exception:
        return "🔴  KI-Suche offline"


# ── Server-Prozess ────────────────────────────────────────────────────────────

_server_proc: subprocess.Popen | None = None
_server_lock = threading.Lock()


def _env() -> dict:
    env = os.environ.copy()
    if _IN_BUNDLE:
        env["ARCHIVIO_DATA_DIR"] = str(_DATA_DIR)
    # launchd startet Login-Items/LaunchAgents mit einer minimalen Umgebung ohne
    # LANG/LC_ALL -- ohne diese Variablen faellt Python teils auf ASCII statt UTF-8
    # fuer stdout/stderr/Log-Dateien zurueck. Jede Log-Zeile mit Umlauten (Postfach-
    # oder Dateinamen wie "Möbelierung") liess den Mail-/Datei-Scan dann mit
    # UnicodeEncodeError abbrechen. PYTHONUTF8 erzwingt UTF-8 unabhaengig vom
    # (evtl. leeren) Locale-Environment.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _prepare_data_dir():
    """Erstellt Datenverzeichnis und initialisiert config.yaml + DB beim ersten Start."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    (_DATA_DIR / "logs").mkdir(exist_ok=True)

    config = _DATA_DIR / "config.yaml"
    if not config.exists() and _EXAMPLE.exists():
        shutil.copy(_EXAMPLE, config)
        log.info("config.yaml aus Beispiel erstellt: %s", config)

    db = _DATA_DIR / "archivio.db"
    if not db.exists():
        python = sys.executable if _IN_BUNDLE else str(_VENV / "bin" / "python3")
        schema = _CODE_ROOT / "db" / "schema.sql"
        log.info("Datenbank initialisieren: %s", db)
        subprocess.run(
            [python, "-c",
             "import sys; sys.path.insert(0,'.');from db import connection; connection.init_schema()"],
            cwd=str(_CODE_ROOT),
            env=_env(),
            timeout=30,
        )


def _kill_port_8000():
    """Beendet alle Prozesse die Port 8000 belegen (Überbleibsel älterer Instanzen)."""
    try:
        r = subprocess.run(["lsof", "-ti", ":8000"], capture_output=True, text=True)
        for pid in r.stdout.strip().split():
            try:
                subprocess.run(["kill", "-9", pid], capture_output=True)
                log.info("Alten Prozess auf Port 8000 beendet: PID %s", pid)
            except Exception:
                pass
    except Exception:
        pass


def _probe_permissions():
    """Ruft Diagnose-Endpoint auf sobald der Server bereit ist.
    Triggert macOS-Berechtigungsdialoge (Schreibtisch, FDA) beim ersten Start."""
    if _wait_for_server(timeout=60):
        try:
            requests.get("http://127.0.0.1:8000/api/debug/diagnostics", timeout=30)
        except Exception:
            pass


def _start_server():
    global _server_proc
    with _server_lock:
        if _server_proc and _server_proc.poll() is None:
            return
        _kill_port_8000()
        if _IN_BUNDLE:
            _prepare_data_dir()
        python = sys.executable if _IN_BUNDLE else str(_VENV / "bin" / "python3")
        log_path = (_DATA_DIR / "logs" / "server.log") if _IN_BUNDLE else (_CODE_ROOT / "logs" / "server.log")
        log_path.parent.mkdir(exist_ok=True)
        log_file = open(log_path, "a")
        _server_proc = subprocess.Popen(
            [python, "-m", "uvicorn", "web.main:app",
             "--host", "0.0.0.0", "--port", "8000"],
            cwd=str(_CODE_ROOT),
            env=_env(),
            stdout=log_file,
            stderr=log_file,
        )
        log.info("uvicorn gestartet (pid %s)", _server_proc.pid)


def _stop_server():
    global _server_proc
    with _server_lock:
        if _server_proc:
            try:
                _server_proc.terminate()
                _server_proc.wait(timeout=8)
            except Exception as e:
                log.warning("Server stop: %s", e)
                _server_proc.kill()
            _server_proc = None
            log.info("uvicorn gestoppt")


def _notify(message: str):
    """macOS-Benachrichtigung aus beliebigem Thread."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "Archivio Server"'],
            timeout=5,
        )
    except Exception:
        pass


_SERVER_RAM_LIMIT_GB = 20.0  # uvicorn-Prozess-RAM-Limit (ohne Worker-Prozesse)


def _scan_state() -> tuple[bool, bool]:
    """Gibt (projekt_scan_laeuft, mail_scan_laeuft) zurück."""
    try:
        state = requests.get("http://127.0.0.1:8000/api/scan/state", timeout=3).json()
        proj = any(s.get("status") == "running" for s in state.get("scans", {}).values())
        mail = state.get("mail_scan", {}).get("status") == "running"
        return proj, mail
    except Exception:
        return False, False


def _wait_for_server(timeout: int = 40) -> bool:
    """Wartet bis der neue Server-Prozess antwortet."""
    for _ in range(timeout):
        try:
            requests.get("http://127.0.0.1:8000/api/status", timeout=2)
            return True
        except Exception:
            time.sleep(1)
    return False


def _server_responds(timeout: float = 5) -> bool:
    """Schneller Health-Check: antwortet der Server auf HTTP?"""
    try:
        return requests.get("http://127.0.0.1:8000/api/status", timeout=timeout).status_code == 200
    except Exception:
        return False


def _restart_server(resume_projects: bool = False, resume_mail: bool = False,
                    reason: str = "") -> bool:
    """Stoppt (hart, inkl. Port-Freigabe) und startet den Server neu, wartet bis
    er antwortet, und setzt den RICHTIGEN Scan-Typ fort. Gibt True zurück wenn der
    Server danach erreichbar ist.

    Wichtig: Lief nur ein MAIL-Scan, wird auch nur der Mail-Scan fortgesetzt —
    NICHT /api/scan/all (das würde alle Projekte scannen, obwohl der Nutzer nur
    Postfächer gescannt hat).
    """
    _stop_server()
    time.sleep(5)
    _start_server()
    if not _wait_for_server():
        log.error("Server nach Neustart nicht erreichbar (%s)", reason or "?")
        _notify("⚠ Archivio-Server-Neustart fehlgeschlagen — neuer Versuch folgt")
        return False
    log.info("Server nach Neustart erreichbar (%s)", reason or "?")

    try:
        if resume_projects:
            # /api/scan/all deckt Projekte UND Postfächer ab
            r = requests.post("http://127.0.0.1:8000/api/scan/all", timeout=10)
            log.info("Projekt-Scan nach Neustart fortgesetzt: HTTP %s", r.status_code)
            _notify("Scan läuft weiter — bereits Verarbeitetes wird übersprungen")
        elif resume_mail:
            # Nur der Mail-Scan lief → nur diesen fortsetzen
            r = requests.post("http://127.0.0.1:8000/dashboard/mail/scan", timeout=10)
            log.info("Mail-Scan nach Neustart fortgesetzt: HTTP %s", r.status_code)
            _notify("Mail-Scan läuft weiter")
    except Exception as exc:
        log.warning("Scan-Resume fehlgeschlagen: %s", exc)
    return True


def _server_memory_watchdog():
    """Überwacht den Server alle 15s auf DREI Arten:
      1. Prozess tot           → sofort neu starten (Crash, fehlgeschlagener
                                  Neustart, vom OS gekillt). Ohne das blieb der
                                  Server nach einem misslungenen Neustart tagelang
                                  tot, weil die Schleife nur bei RAM-Ueberschreitung
                                  aktiv wurde.
      2. Prozess lebt, haengt  → nach 4 erfolglosen Health-Checks (60s) neu starten
                                  (vermeidet Fehlalarme bei kurzer Last).
      3. RSS > Limit           → kontrollierter Neustart mit Scan-Fortsetzung.
    """
    try:
        import psutil
    except ImportError:
        log.warning("psutil nicht verfügbar — Server-Watchdog deaktiviert")
        return

    unresponsive = 0
    last_proj = last_mail = False   # zuletzt bekannter Scan-Stand (für Resume nach Crash)
    while True:
        time.sleep(15)
        try:
            alive = bool(_server_proc and _server_proc.poll() is None)

            # 1) Prozess tot → sofort neu starten (Resume gemäß letztem bekannten Stand)
            if not alive:
                log.warning("Server-Prozess nicht aktiv — Neustart")
                _notify("Archivio-Server war nicht aktiv — wird neu gestartet")
                _restart_server(resume_projects=last_proj, resume_mail=last_mail,
                                reason="Prozess tot")
                unresponsive = 0
                continue

            # 2) Prozess lebt, antwortet aber nicht (erst nach mehreren Fehlversuchen)
            if not _server_responds():
                unresponsive += 1
                log.warning("Server antwortet nicht (%d/4)", unresponsive)
                if unresponsive >= 4:
                    log.warning("Server haengt — Neustart")
                    _restart_server(resume_projects=last_proj, resume_mail=last_mail,
                                    reason="haengt")
                    unresponsive = 0
                continue
            unresponsive = 0

            # Aktuellen Scan-Stand merken (für Resume, falls gleich ein Neustart nötig wird)
            last_proj, last_mail = _scan_state()

            # 3) RAM-Grenze → typgerecht fortsetzen (nur Mail wenn nur Mail lief!)
            rss_gb = psutil.Process(_server_proc.pid).memory_info().rss / (1024 ** 3)
            if rss_gb > _SERVER_RAM_LIMIT_GB:
                log.warning("Server-RAM %.1f GB > %.1f GB — automatischer Neustart "
                            "(Projekt-Scan=%s, Mail-Scan=%s)",
                            rss_gb, _SERVER_RAM_LIMIT_GB, last_proj, last_mail)
                _notify(f"Neustart (RAM: {rss_gb:.0f} GB)")
                _restart_server(resume_projects=last_proj, resume_mail=last_mail,
                                reason=f"RAM {rss_gb:.0f} GB")
        except Exception as exc:
            log.warning("Server-Watchdog: %s", exc)


# ── Version ───────────────────────────────────────────────────────────────────

def _local_version() -> str:
    try:
        return _VERSION.read_text().strip()
    except Exception:
        return "0.0.0"


# ── Update ────────────────────────────────────────────────────────────────────

def _last_notified_version() -> str | None:
    try:
        return json.loads(_UPDATE_STATE.read_text()).get("notified_version")
    except Exception:
        return None


def _remember_notified_version(version: str):
    try:
        _UPDATE_STATE.parent.mkdir(parents=True, exist_ok=True)
        _UPDATE_STATE.write_text(json.dumps({"notified_version": version}))
    except Exception as e:
        log.warning("Update-Status konnte nicht gespeichert werden: %s", e)


def _update_watchdog(app: "ArchivioServer"):
    time.sleep(60)
    while True:
        try:
            info = updater.pruefe_update(_local_version(), log)
            if info:
                app._on_update_found(info)
        except Exception as e:
            log.warning("Update-Watchdog: %s", e)
        time.sleep(24 * 60 * 60)


# ── App ───────────────────────────────────────────────────────────────────────

_ICON = str(_HERE / "icon.png")


class ArchivioServer(rumps.App):
    def __init__(self):
        super().__init__("", icon=_ICON, template=True, quit_button=None)

        self._title_item    = rumps.MenuItem("Archivio Server")
        self._version_item  = rumps.MenuItem(f"Version {_local_version()}")
        self._server_item   = rumps.MenuItem("⬤  Server …")
        self._nas_item      = rumps.MenuItem("⬤  NAS …")
        self._ki_item       = rumps.MenuItem("⬤  KI-Suche …", callback=self._ki_action)
        self._autostart_item = rumps.MenuItem(
            "Autostart beim Login", callback=self.toggle_autostart)
        self._link_action_item = rumps.MenuItem(
            self._link_action_title(), callback=self.toggle_link_action)
        self._update_item = rumps.MenuItem(
            "Update verfügbar", callback=self._install_pending_update)
        self._pending_update_info: updater.UpdateInfo | None = None
        self._zc = self._zc_info = None

        self.menu = [
            self._title_item,
            rumps.separator,
            self._version_item,
            self._server_item,
            self._nas_item,
            self._ki_item,
            rumps.separator,
            self._autostart_item,
            self._link_action_item,
            rumps.separator,
            rumps.MenuItem("Archivio öffnen",    callback=self.open_browser),
            rumps.MenuItem("Auf Updates prüfen", callback=self.check_update),
            rumps.separator,
            rumps.MenuItem("Beenden", callback=self.quit_app),
        ]
        self.menu.insert_before("Archivio Server", self._update_item)
        self._update_item.hidden = True
        # Reparatur EINMALIG beim Start — kaputte Login-Items (frueherer Pfad-Bug,
        # zeigen auf Contents statt die .app, oeffnen bei jedem Login ein
        # Finder-Fenster) entfernen. Ohne diese Reparatur wuerde ein kaputter Eintrag
        # NIE von selbst verschwinden: er startet die App ja nicht (nur Finder),
        # kann also auch keinen eigenen Reparatur-Code ausloesen — erst das manuelle
        # Oeffnen der App (dieser Code-Pfad hier) heilt ihn.
        bridge.repair_broken_autostart_entries("Archivio Server", log)
        self._autostart_item.state = bridge.ensure_autostart_default(log, _UPDATE_STATE)

        # config_provider liefert die eigene Adresse — dieselbe fest verdrahtete
        # localhost:8000, die dieser Prozess an anderen Stellen (_wait_for_server,
        # _server_responds) bereits verwendet. Ermoeglicht archivio_mcp.py, das jetzt
        # auch im Server-Bundle liegt, sich selbst darueber aufzuloesen.
        bridge.start_local_server(
            "Archivio Server", log,
            config_provider=lambda: "http://127.0.0.1:8000",
            link_action_provider=self._link_action,
        )
        bridge.register_url_handler(log)
        bridge.ensure_mcp_registered("Archivio Server", log)
        bridge.ensure_quick_action_installed(log)
        threading.Thread(target=self._boot, daemon=True).start()

    def _boot(self):
        _start_server()
        threading.Thread(target=_ensure_ollama_models, daemon=True).start()
        threading.Thread(target=_server_memory_watchdog, daemon=True).start()
        threading.Thread(target=_probe_permissions, daemon=True).start()
        threading.Thread(target=_update_watchdog, args=(self,), daemon=True).start()
        self._zc, self._zc_info = bridge.advertise_service(8000, log)
        threading.Thread(target=self._check_other_servers, daemon=True).start()
        time.sleep(3)
        self._status_loop()

    def _check_other_servers(self):
        """Einmalig beim Start: prüft per mDNS, ob bereits ein anderer Archivio-Server
        im Netz läuft, und weist per Dialog darauf hin -- mehrere Server bleiben
        technisch möglich, sind aber i.d.R. nicht sinnvoll (jeder scannt die NAS-Dateien
        separat statt sich einen gemeinsamen Index zu teilen)."""
        time.sleep(5)  # eigenen mDNS-Dienst zuerst propagieren lassen
        others = bridge.discover_other_servers(8000, timeout=3, log=log)
        if not others:
            return
        names = ", ".join(sorted({hostname for _, _, hostname in others}))
        bridge.thread_alert(
            "Weiterer Archivio-Server im Netzwerk gefunden",
            f"Es gibt bereits andere Nutzer im Netzwerk mit einem Archivio-Server. "
            f"Gerätename: {names}. Es macht in der Regel Sinn, einen gemeinsamen Server "
            f"für alle zu betreiben und auf den Arbeitsstationen nur den Archivio Helper "
            f"zu installieren -- damit müssen die Dateien nicht von allen Nutzern einzeln "
            f"gescannt werden. Es ist aber grundsätzlich möglich, mehrere Server parallel "
            f"zu nutzen.",
        )

    def _status_loop(self):
        while True:
            self._refresh_status()
            time.sleep(30)

    def _refresh_status(self):
        try:
            data = requests.get("http://127.0.0.1:8000/api/status", timeout=3).json()
            server_ok = data.get("server", False)
            nas_ok    = data.get("nas", False)
        except Exception:
            server_ok = False
            nas_ok    = False
        self._server_item.title = (
            f"{'🟢' if server_ok else '🔴'}  Server {'läuft' if server_ok else 'offline'}")
        self._nas_item.title = (
            f"{'🟢' if nas_ok else '🔴'}  NAS {'verbunden' if nas_ok else 'nicht verbunden'}")
        self._ki_item.title = _ollama_status_label()
        # Ollama neu starten falls es unerwartet gestoppt ist
        if _ollama_available() and not _is_ollama_running():
            threading.Thread(target=_start_ollama, daemon=True).start()

    def open_browser(self, _):
        subprocess.run(["open", "http://127.0.0.1:8000"])

    def toggle_autostart(self, sender):
        new_state = sender.state != 1
        bridge.set_autostart(new_state, log)
        bridge.save_autostart_preference(_UPDATE_STATE, new_state, log)
        sender.state = new_state

    def _link_action(self) -> str:
        try:
            return json.loads(_UPDATE_STATE.read_text()).get("link_action", "open")
        except Exception:
            return "open"

    def _link_action_title(self) -> str:
        return ("Archivio-Links: Direkt öffnen" if self._link_action() == "open"
                else "Archivio-Links: Zum Pfad gehen")

    def toggle_link_action(self, sender):
        """Wechselt das Standardverhalten fuer per Quick Action kopierte Links (Landing
        Page unter /link, siehe shared/menubar_bridge.py _link_landing_page) zwischen
        Direkt-Oeffnen und Im-Finder-Zeigen."""
        new_action = "reveal" if self._link_action() == "open" else "open"
        try:
            state = {}
            if _UPDATE_STATE.exists():
                state = json.loads(_UPDATE_STATE.read_text())
            state["link_action"] = new_action
            _UPDATE_STATE.parent.mkdir(parents=True, exist_ok=True)
            _UPDATE_STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        except Exception as e:
            log.warning("Link-Verhalten konnte nicht gespeichert werden: %s", e)
        sender.title = self._link_action_title()
        sender.state = new_state

    def _ki_action(self, _):
        if not _ollama_available():
            subprocess.run(["open", "https://ollama.com/download"])
        elif not _is_ollama_running():
            rumps.notification("Archivio", "KI-Suche", "Ollama wird gestartet…")
            threading.Thread(target=_start_ollama, daemon=True).start()

    def check_update(self, _):
        current = _local_version()
        info = updater.pruefe_update(current, log)
        log.info("Update-Check (manuell): lokal=%s gefunden=%s", current,
                  info.version if info else None)
        if not info:
            rumps.alert(f"Archivio Server {current} ist aktuell oder Prüfung nicht möglich.")
            return
        self._offer_install(info)

    def _on_update_found(self, info: updater.UpdateInfo):
        self._update_item.title = f"⬆ Update auf v{info.version} verfügbar"
        self._update_item.hidden = False
        self._pending_update_info = info
        if _last_notified_version() != info.version:
            _notify(f"Update auf v{info.version} verfügbar")
            _remember_notified_version(info.version)

    def _offer_install(self, info: updater.UpdateInfo):
        if not rumps.alert(
            title="Update verfügbar",
            message=f"Version {info.version} verfügbar (aktuell: {_local_version()}).\n"
                    f"Jetzt herunterladen und installieren?",
            ok="Installieren", cancel="Abbrechen",
        ):
            return
        # Download+Verifikation im Hintergrund -- lade_und_pruefe() kann je nach
        # Verbindung mehrere Sekunden bis Minuten dauern; ohne eigenen Thread blockiert
        # das den Menü-Callback (Hauptthread) und die App wirkt eingefroren.
        self._update_item.title = f"⬇  v{info.version} wird heruntergeladen…"
        threading.Thread(target=self._download_and_install, args=(info,), daemon=True).start()

    def _download_and_install(self, info: updater.UpdateInfo):
        pkg = updater.lade_und_pruefe(info, log)
        if not pkg:
            # rumps.alert() braucht den Hauptthread -- hier stattdessen der bestehende
            # thread-sichere _notify()-Helper (osascript). Menü-Item bleibt sichtbar
            # und klickbar, ein erneuter Versuch ist über das Menü jederzeit möglich.
            self._update_item.title = f"⬆ Update auf v{info.version} verfügbar"
            _notify(f"Update-Download für v{info.version} fehlgeschlagen — erneut über das Menü versuchen")
            log.warning("Update-Download/-Verifikation fehlgeschlagen: v%s", info.version)
            return
        self._update_item.title = f"✓  v{info.version} wird installiert…"
        updater.installiere(pkg)

    def _install_pending_update(self, _):
        if self._pending_update_info:
            self._offer_install(self._pending_update_info)

    def quit_app(self, _):
        bridge.stop_advertising(self._zc, self._zc_info, log)
        _stop_server()
        _stop_ollama()
        # Agent entladen, sonst startet KeepAlive=true die App sofort neu.
        # bootout beendet zugleich diesen Prozess.
        try:
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/io.archivio.server"],
                           timeout=5)
        except Exception:
            pass
        rumps.quit_application()


if __name__ == "__main__":
    try:
        ArchivioServer().run()
    except Exception as e:
        log.exception("Fatal error: %s", e)
        raise
