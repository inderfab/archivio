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
from urllib.parse import parse_qs, quote, unquote, urlparse

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


def _link_landing_page(app_name: str, path: str, action: str) -> bytes:
    """Zwischenseite fuer per Quick Action kopierte Archivio-Links: GET auf /link loest
    NIE direkt eine Aktion aus (im Unterschied zu /open, /reveal) -- sonst reicht ein
    Link-Preview/Unfurling der Ziel-App (z.B. Mail.app faengt beim Einfuegen automatisch
    an, eine Vorschau des Links zu laden) um die Datei ungewollt zu oeffnen. Erst der
    tatsaechliche Klick auf den Button hier loest /open bzw. /reveal aus -- ein
    automatisierter Preview-Fetch klickt nichts."""
    action = action if action in ("open", "reveal") else "open"
    label  = "Datei öffnen" if action == "open" else "Im Finder zeigen"
    enc    = quote(path, safe="")
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{app_name}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; display:flex; align-items:center;
          justify-content:center; height:100vh; margin:0; background:#f5f5f5; color:#333; }}
  .box {{ text-align:center; }}
  .path {{ color:#888; font-size:12px; margin-bottom:16px; word-break:break-all; max-width:400px; }}
  a.btn {{ display:inline-block; padding:10px 22px; background:#2563eb; color:#fff;
           text-decoration:none; border-radius:6px; font-size:14px; }}
</style></head>
<body><div class="box">
<div class="path">{path}</div>
<a class="btn" href="/{action}?path={enc}">{label}</a>
</div></body></html>"""
    return html.encode("utf-8")


def _unique_dest(dest_dir: Path, filename: str) -> Path:
    """Verhindert Überschreiben bei Namenskollision im Zielordner (z.B. gleicher
    Dateiname aus zwei verschiedenen Projektordnern in einer Auswahl)."""
    target = dest_dir / filename
    if not target.exists():
        return target
    stem, suffix = Path(filename).stem, Path(filename).suffix
    i = 2
    while True:
        candidate = dest_dir / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def make_local_http_handler(app_name: str, log, config_provider=None, link_action_provider=None):
    """config_provider: optionales Callable[[], str], das server_url für den
    /config-Endpoint liefert. Helper übergibt einen Reader auf sein config.json,
    Server übergibt einen fest verdrahteten "http://127.0.0.1:8000" (dieselbe Adresse,
    die server_app.py an anderen Stellen bereits hardcoded verwendet).
    link_action_provider: optionales Callable[[], str] ("open"/"reveal"), liefert das
    im Menü eingestellte Standardverhalten für /link (Quick-Action-Links)."""

    class _LocalHTTPHandler(http.server.BaseHTTPRequestHandler):
        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path == "/copy-to-folder":
                length = int(self.headers.get("Content-Length", 0) or 0)
                try:
                    raw = self.rfile.read(length) if length else b"{}"
                    body = json.loads(raw or b"{}")
                except Exception:
                    body = {}
                self._handle_copy_to_folder(body.get("paths") or [])
            else:
                self._cors_headers(404)

        def _handle_copy_to_folder(self, paths):
            """Öffnet einen nativen Finder-Ordner-Picker (choose folder) und kopiert
            die übergebenen Dateien dorthin -- für "Auswahl in neuen Ordner speichern"
            im Foto-Browser (z.B. Fotoauswahl für eine Besprechung zusammenstellen)."""
            if not paths:
                self._json_response(400, {"ok": False, "error": "Keine Dateien ausgewählt"})
                return
            try:
                result = subprocess.run(
                    ["osascript", "-e",
                     'POSIX path of (choose folder with prompt "Zielordner für die Auswahl wählen")'],
                    capture_output=True, text=True, timeout=300,
                )
            except Exception as e:
                self._json_response(500, {"ok": False, "error": str(e)})
                return
            if result.returncode != 0:
                self._json_response(200, {"ok": False, "cancelled": True})
                return
            dest_dir = Path(result.stdout.strip())
            copied, errors = 0, []
            for p in paths:
                src = Path(p)
                try:
                    if not src.exists():
                        errors.append(f"{src.name}: nicht gefunden")
                        continue
                    target = _unique_dest(dest_dir, src.name)
                    shutil.copy2(src, target)
                    copied += 1
                except Exception as e:
                    errors.append(f"{src.name}: {e}")
            log.info("copy-to-folder: %d/%d nach %s kopiert", copied, len(paths), dest_dir)
            self._json_response(200, {"ok": True, "folder": str(dest_dir), "copied": copied, "errors": errors})

        def _json_response(self, code, payload):
            self._cors_headers(code, json.dumps(payload).encode("utf-8"), "application/json")

        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            path = unquote(params.get("path", [""])[0])

            if parsed.path == "/link" and path:
                action = link_action_provider() if link_action_provider else "open"
                self._cors_headers(200, _link_landing_page(app_name, path, action), "text/html; charset=utf-8")
            elif parsed.path == "/open" and path:
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
            self._cors_headers(code, _html_page(app_name, message, autoclose), "text/html; charset=utf-8")

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


def start_local_server(app_name: str, log, config_provider=None, link_action_provider=None) -> None:
    handler_cls = make_local_http_handler(app_name, log, config_provider, link_action_provider)
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


def ensure_autostart_default(log, state_path: Path) -> bool:
    """Stellt sicher, dass Autostart aktiv ist, ausser der Nutzer hat es bewusst
    abgeschaltet. Noetig weil eine .pkg-Neuinstallation den kompletten .app-Ordner
    ersetzt -- macOS' Login-Item-Mechanismus (System Events) verwirft dabei den Alias
    auf die alte Datei, autostart_enabled() faellt nach jedem Update stillschweigend
    auf False zurueck, obwohl der Nutzer nichts geaendert hat. Die Praeferenz wird
    deshalb separat persistiert (state_path, JSON, Key "autostart_preference") statt
    sich nur auf den fluechtigen macOS-Zustand zu verlassen. Frischer Erststart ohne
    gespeicherte Praeferenz => Default an. Gibt den finalen Zustand fuer die
    Menu-Checkbox zurueck."""
    try:
        pref = json.loads(state_path.read_text()).get("autostart_preference", True)
    except Exception:
        pref = True
    actual = autostart_enabled()
    if pref and not actual:
        set_autostart(True, log)
        actual = True
    return actual


def save_autostart_preference(state_path: Path, enabled: bool, log) -> None:
    """Merged die Praeferenz in state_path, ohne andere dort gespeicherte Keys
    anzutasten (Server nutzt dieselbe Datei z.B. auch fuer Update-Benachrichtigungen)."""
    try:
        state = {}
        if state_path.exists():
            state = json.loads(state_path.read_text())
        state["autostart_preference"] = enabled
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    except Exception as e:
        log.warning("Autostart-Praeferenz konnte nicht gespeichert werden: %s", e)


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


_CLAUDE_APP_PATH = Path("/Applications/Claude.app")


def ensure_mcp_registered(app_name: str, log) -> None:
    if not CLAUDE_CONFIG_PATH.parent.exists():
        # Ordner existiert erst, sobald Claude Desktop mindestens einmal geoeffnet
        # wurde -- ohne diesen Fallback bleibt die Registrierung fuer jeden, der
        # Claude Desktop installiert aber noch nie gestartet hat, dauerhaft und
        # unbemerkt aus (kein Fehler, keine Meldung). Ist die App gar nicht
        # installiert, gibt es nichts zu tun.
        if not _CLAUDE_APP_PATH.exists():
            return
        try:
            CLAUDE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.warning("Claude-Konfigurationsordner konnte nicht angelegt werden: %s", e)
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


# ── Netzwerk-Discovery (mDNS/Bonjour via zeroconf) ────────────────────────────
# Server meldet sich per advertise_service() im LAN an, Helper findet ihn per
# discover_servers() -- ersetzt das fruehere Vorbelegen der server_url beim
# Zip-Download (brach dort die Codesignatur, siehe PROJEKT_STATUS.md).

SERVICE_TYPE = "_archivio._tcp.local."


def _local_ip() -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def advertise_service(port: int, log) -> tuple[object, object] | tuple[None, None]:
    """Meldet diesen Mac als Archivio-Server per mDNS an. Rueckgabe (zeroconf, info)
    wird fuer stop_advertising() gebraucht; (None, None) bei jedem Fehler --
    Discovery darf den Serverstart nie verhindern."""
    try:
        import socket
        from zeroconf import ServiceInfo, Zeroconf

        hostname = socket.gethostname()
        info = ServiceInfo(
            SERVICE_TYPE,
            f"Archivio Server auf {hostname}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(_local_ip())],
            port=port,
            server=f"{hostname}.local.",
        )
        zc = Zeroconf()
        zc.register_service(info)
        log.info("mDNS-Dienst angemeldet: %s:%d", hostname, port)
        return zc, info
    except Exception as e:
        log.warning("mDNS-Anmeldung fehlgeschlagen: %s", e)
        return None, None


def stop_advertising(zc, info, log) -> None:
    if not zc:
        return
    try:
        if info:
            zc.unregister_service(info)
        zc.close()
        log.info("mDNS-Dienst abgemeldet")
    except Exception as e:
        log.warning("mDNS-Abmeldung fehlgeschlagen: %s", e)


def discover_servers(timeout: float, log) -> list[tuple[str, int]]:
    """Sucht `timeout` Sekunden nach Archivio-Servern im LAN. Gibt Liste von
    (host, port) zurueck, leer wenn keiner gefunden wurde oder bei Fehler."""
    try:
        import socket
        import time as _time
        from zeroconf import ServiceBrowser, Zeroconf

        found: list[tuple[str, int]] = []

        class _Listener:
            def add_service(self, zc, type_, name):
                try:
                    info = zc.get_service_info(type_, name)
                    if info and info.addresses:
                        host = socket.inet_ntoa(info.addresses[0])
                        found.append((host, info.port))
                except Exception as e:
                    log.warning("mDNS-Service-Aufloesung fehlgeschlagen: %s", e)

            def remove_service(self, zc, type_, name):
                pass

            def update_service(self, zc, type_, name):
                pass

        zc = Zeroconf()
        try:
            ServiceBrowser(zc, SERVICE_TYPE, _Listener())
            _time.sleep(timeout)
        finally:
            zc.close()
        found = list(dict.fromkeys(found))  # add_service kann pro Dienst mehrfach feuern
        log.info("mDNS-Suche abgeschlossen: %d Server gefunden", len(found))
        return found
    except Exception as e:
        log.warning("mDNS-Suche fehlgeschlagen: %s", e)
        return []


def resolve_discovery(found: list[tuple[str, int]]) -> tuple[str, str | None]:
    """Reine Entscheidungslogik fuer discover_servers()-Ergebnisse, ohne rumps/GUI-
    Abhaengigkeit (damit ohne die menubar-App importierbar/testbar). Gibt
    (decision, url) zurueck: decision in {"none", "one", "multiple"}, url nur bei
    "one" gesetzt."""
    if len(found) == 1:
        host, port = found[0]
        return "one", f"http://{host}:{port}"
    if len(found) > 1:
        return "multiple", None
    return "none", None


def thread_alert(title: str, message: str) -> None:
    msg = message.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    subprocess.run(["osascript", "-e", f'display alert "{title}" message "{msg}"'], timeout=60)
