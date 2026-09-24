from __future__ import annotations

import logging
import logging.handlers
import os
import re
import signal
import subprocess
import sys
import threading
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Logging ───────────────────────────────────────────────────────────────────
# Konfiguriert den Root-Logger so dass Scanner/Extractor-Logs in server.log
# erscheinen (dasselbe File das uvicorn via stdout/stderr beschreibt).

def _setup_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return  # bereits konfiguriert (z.B. im Dev-Modus via --reload)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # uvicorn-Logger nicht doppelt ausgeben
    logging.getLogger("uvicorn.access").propagate = False

_setup_logging()

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from db import connection
from web.shared import templates
from web.dashboard import router as dashboard_router
from web.api import router as api_router
from web.gallery import router as gallery_router


# ── Scheduler ─────────────────────────────────────────────────────────────────

def _scheduler_loop():
    log = logging.getLogger("scheduler")
    triggered_today: str | None = None
    mcp_log_cleaned_today: str | None = None
    search_log_cleaned_today: str | None = None
    backup_ran_today: str | None = None
    log.info("Scheduler-Loop gestartet")
    while True:
        try:
            from config import settings

            today = datetime.now().strftime("%Y-%m-%d")
            if mcp_log_cleaned_today != today:
                mcp_log_cleaned_today = today
                from scanner.mcp_log import cleanup_old
                retention = int(settings.get("mcp_log.retention_days", 365) or 0)
                _conn = connection.get_connection()
                try:
                    deleted = cleanup_old(_conn, retention)
                    if deleted:
                        log.info("MCP-Log: %d Zeile(n) älter als %d Tage gelöscht", deleted, retention)
                finally:
                    _conn.close()

            if search_log_cleaned_today != today:
                search_log_cleaned_today = today
                from scanner.search_log import cleanup_old as cleanup_old_search
                retention = int(settings.get("search_log.retention_days", 180) or 0)
                _conn = connection.get_connection()
                try:
                    deleted = cleanup_old_search(_conn, retention)
                    if deleted:
                        log.info("Suche-Log: %d Zeile(n) älter als %d Tage gelöscht", deleted, retention)
                finally:
                    _conn.close()

            scan_time = (settings.get("scheduler.scan_time") or "").strip()
            if scan_time:
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                # 2-Minuten-Fenster: verhindert dass sleep(60)-Drift die Minute verpasst
                h, m = map(int, scan_time.split(":"))
                target = now.replace(hour=h, minute=m, second=0, microsecond=0)
                diff = abs((now - target).total_seconds())
                if diff < 120 and triggered_today != today:
                    triggered_today = today
                    port = settings.get("server.port", 8000)
                    log.info("Geplanter Scan um %s wird ausgelöst (Port %s)", scan_time, port)
                    try:
                        import requests as _req
                        r = _req.post(f"http://127.0.0.1:{port}/api/scan/all", timeout=10)
                        log.info("Geplanter Scan gestartet: HTTP %s %s", r.status_code, r.text[:200])
                    except Exception as exc:
                        log.error("Geplanter Scan fehlgeschlagen: %s", exc)

            backup_ran_today = _maybe_run_weekly_backup(log, backup_ran_today)
        except Exception as exc:
            log.error("Scheduler-Fehler: %s", exc)
        time.sleep(60)


def _maybe_run_weekly_backup(log, ran_today: str | None) -> str | None:
    """Wöchentliche Sicherung, eine Stunde nach dem Nachtscan.

    Der Versatz ist Absicht: so ist der Scan durch und die Sicherung enthält den
    frischen Stand. Läuft trotzdem noch einer (grosse Bestände dauern), wird auf den
    nächsten Tick verschoben statt übersprungen — eine Sicherung parallel zum Scan
    ist zwar möglich, kostet aber unnötig I/O auf derselben Platte."""
    from config import settings
    from db import backup as backup_mod
    from web.api import _backup_state
    from web.dashboard import _scans

    target = (settings.get("backup.path") or "").strip()
    if not target or not settings.get("backup.enabled", True):
        return ran_today

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if ran_today == today or now.weekday() != int(settings.get("backup.weekday", 6) or 0):
        return ran_today

    scan_time = (settings.get("scheduler.scan_time") or "22:00").strip() or "22:00"
    h, m = map(int, scan_time.split(":"))
    target_time = (now.replace(hour=h, minute=m, second=0, microsecond=0)
                   + timedelta(hours=1))
    # Bewusst kein enges Zeitfenster wie beim Scan: blockiert ein laufender Scan die
    # Sicherung, soll sie danach nachgeholt werden und nicht eine Woche ausfallen.
    if now < target_time:
        return ran_today

    if _backup_state.get("running"):
        return ran_today
    if any(s.get("status") == "running" for s in _scans.values()):
        log.info("Sicherung verschoben — es läuft noch ein Scan")
        return ran_today

    log.info("Wöchentliche Sicherung wird gestartet: %s", target)
    threading.Thread(
        target=lambda: backup_mod.create_backup(target, _backup_state), daemon=True
    ).start()
    return today


# ── Startvorbereitung ─────────────────────────────────────────────────────────
# Zustand der einmaligen Vorbereitung beim Serverstart (Schema + Migrationen).

# "laeuft" startet bewusst auf False und wird erst im lifespan gesetzt: der Wert
# bedeutet "die Vorbereitung laeuft gerade", nicht "sie steht noch aus". Andernfalls
# antwortete die App ueberall dort mit der Warteseite, wo der lifespan gar nicht
# ausgefuehrt wird -- etwa in den Tests, die die App direkt einbinden.
_start_zustand: dict = {"laeuft": False, "seit": time.time(), "fehler": None}


def _aufraeumen_nach_migration(conn) -> None:
    """Einmaliges VACUUM, wenn eine Migration Platz freigegeben hat.

    `ALTER TABLE ... DROP COLUMN` loescht die Daten aus der Tabelle, gibt die Seiten
    aber nicht an das Dateisystem zurueck -- ohne VACUUM bleibt die Datei gleich gross.
    Bei Migration 029 sind das auf einer Buero-Datenbank rund 1 GB.

    Laeuft hier und nicht in der Migration selbst, weil VACUUM die Datenbank waehrend
    des Laufs ein zweites Mal auf die Platte schreibt: der Platz muss vorher geprueft
    werden, und der Vorgang gehoert hinter die Warteseite. /api/status bleibt dabei
    erreichbar, der Watchdog schlaegt also nicht zu.
    """
    log = logging.getLogger(__name__)
    marke = conn.execute(
        "SELECT 1 FROM _migrations WHERE id = '029_vacuum_offen'").fetchone()
    if not marke:
        return

    db_pfad = connection.db_path()
    groesse = db_pfad.stat().st_size if db_pfad.exists() else 0
    frei    = shutil.disk_usage(db_pfad.parent).free
    if frei < groesse * 1.2:
        log.warning("VACUUM uebersprungen: %.1f GB frei, noetig waeren %.1f GB",
                    frei / 1e9, groesse * 1.2 / 1e9)
        return

    t0 = time.time()
    try:
        conn.execute("VACUUM")
        conn.commit()
    except Exception as exc:
        log.warning("VACUUM fehlgeschlagen: %s", exc)
        return
    conn.execute("DELETE FROM _migrations WHERE id = '029_vacuum_offen'")
    conn.commit()
    nachher = db_pfad.stat().st_size if db_pfad.exists() else 0
    log.info("VACUUM abgeschlossen (%.0f s): %.2f GB -> %.2f GB",
             time.time() - t0, groesse / 1e9, nachher / 1e9)


def _startvorbereitung() -> None:
    """Schema und Migrationen — im Hintergrund, NICHT blockierend.

    Lief das früher direkt im lifespan, nahm uvicorn erst danach Verbindungen an.
    Bei einer grossen Migration (v3.4.0 braucht auf 240'000 Dokumenten rund drei
    Minuten) bedeutete das:

      * Der Nutzer sieht im Browser nur „Verbindung fehlgeschlagen" und weiss nicht,
        ob überhaupt etwas passiert.
      * Schlimmer: der Watchdog der Menüleisten-App prüft alle 15 s /api/status und
        startet den Server nach vier Fehlversuchen (60 s) neu — eine Migration, die
        länger dauert, wäre also mitten drin abgeschossen und von vorn begonnen
        worden, womöglich endlos.

    Jetzt startet der Server sofort, antwortet auf /api/status und zeigt allen
    anderen Aufrufen eine Warteseite, bis die Vorbereitung durch ist.
    """
    try:
        from db import migrations as _mig
        _conn = connection.get_connection()
        _mig.run(_conn)
        _aufraeumen_nach_migration(_conn)
        _conn.close()
    except Exception as _e:
        _start_zustand["fehler"] = str(_e)
        logging.getLogger(__name__).warning("Migration fehlgeschlagen: %s", _e)
    finally:
        _start_zustand["laeuft"] = False
        dauer = time.time() - _start_zustand["seit"]
        logging.getLogger(__name__).info("Startvorbereitung abgeschlossen (%.0f s)", dauer)

    # Erst jetzt die Dienste starten, die die Datenbank benutzen.
    threading.Thread(target=_scheduler_loop, daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _start_zustand.update({"laeuft": True, "seit": time.time(), "fehler": None})
    threading.Thread(target=_startvorbereitung, daemon=True).start()

    # SIGTERM → alle Scanner-Worker sofort killen bevor Prozess endet
    def _sigterm(sig, frame):
        from scanner.walker import kill_all_workers
        kill_all_workers()
        raise SystemExit(0)
    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except Exception:
        pass

    yield
    # Shutdown: alle Worker killen
    from scanner.walker import kill_all_workers
    kill_all_workers()


app = FastAPI(title="Archivio", lifespan=lifespan)


_WARTESEITE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<title>Archivio wird vorbereitet</title>
<meta http-equiv="refresh" content="4">
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
         background:#fafafa; color:#111; display:flex; align-items:center;
         justify-content:center; height:100vh; margin:0; }
  .box { max-width:460px; text-align:center; padding:0 24px; }
  h1 { font-size:20px; font-weight:700; margin:18px 0 10px; }
  p  { font-size:14px; line-height:1.6; color:#4b5563; margin:0 0 10px; }
  .klein { font-size:12px; color:#9ca3af; }
  .kreis { width:34px; height:34px; margin:0 auto; border:3px solid #e5e7eb;
           border-top-color:#111; border-radius:50%; animation:dreh 1s linear infinite; }
  @keyframes dreh { to { transform:rotate(360deg); } }
</style></head>
<body><div class="box">
  <div class="kreis"></div>
  <h1>Archivio wird vorbereitet</h1>
  <p>Die Datenbank wird einmalig für diese Version umgestellt. Das passiert nur nach
     einem Update und kann bei grossen Archiven <strong>einige Minuten</strong> dauern.</p>
  <p>Bitte das Fenster offen lassen — es wechselt von selbst, sobald alles bereit ist.</p>
  <p class="klein">Läuft seit SEKUNDEN Sekunden</p>
</div></body></html>"""

_FEHLERSEITE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><title>Archivio</title>
<style>body{font-family:-apple-system,sans-serif;background:#fafafa;color:#111;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{max-width:520px;padding:0 24px}h1{font-size:19px}p{font-size:14px;line-height:1.6;color:#4b5563}
code{display:block;background:#f3f4f6;padding:8px 10px;border-radius:6px;font-size:12px;
white-space:pre-wrap;margin-top:10px}</style></head>
<body><div class="box"><h1>Archivio konnte nicht vorbereitet werden</h1>
<p>Beim Umstellen der Datenbank ist ein Fehler aufgetreten. Der Server läuft, arbeitet
aber mit einem unfertigen Stand. Bitte den Server einmal neu starten; hilft das nicht,
die letzte Sicherung einspielen.</p><code>FEHLER</code></div></body></html>"""


@app.middleware("http")
async def _vorbereitung_abfangen(request: Request, call_next):
    """Zeigt während der Startvorbereitung eine Warteseite statt einer toten Verbindung.

    /api/status wird bewusst durchgelassen und OHNE Datenbankzugriff beantwortet:
    daran erkennt der Watchdog der Menüleisten-App, dass der Server lebt. Eine
    Abfrage gegen die gerade migrierende Datenbank würde dort bis zum Sperr-Timeout
    hängen und den Server fälschlich als hängend erscheinen lassen.
    """
    if not _start_zustand["laeuft"]:
        return await call_next(request)

    pfad = request.url.path
    if pfad.startswith("/static"):
        return await call_next(request)
    if pfad == "/api/status":
        return JSONResponse({"server": True, "vorbereitung": True,
                             "seit_s": round(time.time() - _start_zustand["seit"])})
    if _start_zustand["fehler"]:
        return HTMLResponse(_FEHLERSEITE.replace("FEHLER", str(_start_zustand["fehler"])),
                            status_code=503)
    return HTMLResponse(
        _WARTESEITE.replace("SEKUNDEN", str(round(time.time() - _start_zustand["seit"]))),
        status_code=503)


# Absoluter Pfad statt "web/static" -- Starlettes StaticFiles löst eine relative
# Angabe bei JEDER Anfrage neu über os.getcwd() auf (siehe lookup_path() in
# starlette/staticfiles.py), statt sie einmal beim Start festzuhalten. Läuft ein
# Update über eine noch laufende alte Instanz drüber (das .pkg ersetzt den
# gesamten "Contents/Resources"-Ordner, bevor das postinstall-Skript den alten
# Prozess beendet), verschwindet dessen Arbeitsverzeichnis für die Dauer der
# Installation unter ihm weg -- os.getcwd() wirft dann ENOENT, und JEDE
# /static/…-Anfrage (inkl. htmx.min.js, logo.svg) endet in einem echten
# "Internal Server Error" statt einem sauberen 404. Andere Routen bemerken das
# nicht, weil sie über ARCHIVIO_DATA_DIR (absolut) statt über das
# Arbeitsverzeichnis auf ihre Dateien zugreifen. templates in web/shared.py
# macht es mit Path(__file__)... bereits richtig -- hier nachgezogen.
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
    name="static",
)
app.include_router(dashboard_router)
app.include_router(api_router)
app.include_router(gallery_router)

# ── Routen ────────────────────────────────────────────────────────────────────

def _build_project_groups(conn) -> list[dict]:
    """Gibt Projekte zurück, annotiert mit Eltern-Info für den Suche-Dropdown."""
    rows = conn.execute(
        "SELECT id, name, path FROM projects WHERE active=1 ORDER BY path"
    ).fetchall()
    projects = [dict(r) for r in rows]
    # Eltern-Projekt bestimmen: längstes path-Prefix das selbst ein Projekt ist
    for p in projects:
        parent = None
        best = 0
        for other in projects:
            if other["id"] == p["id"]:
                continue
            if p["path"].startswith(other["path"] + "/") and len(other["path"]) > best:
                parent = other
                best = len(other["path"])
        p["parent_id"]   = parent["id"]   if parent else None
        p["parent_name"] = parent["name"] if parent else None
    # Kinder pro Eltern sammeln
    children: dict[int, list] = {}
    top: list[dict] = []
    for p in projects:
        if p["parent_id"] is not None:
            children.setdefault(p["parent_id"], []).append(p)
        else:
            top.append(p)
    # Resultat: Top-Level-Projekte mit ihren Kindern
    result = []
    for p in top:
        p["children"] = children.get(p["id"], [])
        result.append(p)
    # Verwaiste Sub-Projekte (Eltern nicht aktiv) ans Ende
    all_top_ids = {p["id"] for p in top}
    for p in projects:
        if p["parent_id"] is not None and p["parent_id"] not in all_top_ids:
            p["children"] = []
            result.append(p)
    return result


def _mailbox_display_name(mailbox_name: str) -> str:
    last = mailbox_name.split("/")[-1].strip()
    return "Inbox" if last.upper() == "INBOX" else last


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    conn = connection.get_connection()
    projects = _build_project_groups(conn)
    raw_mailboxes = conn.execute(
        "SELECT mailbox_name FROM mail_scan_config WHERE active=1 AND project_id IS NULL ORDER BY mailbox_name"
    ).fetchall()
    total_docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    conn.close()
    mail_mailboxes = [
        {"mailbox_name": r["mailbox_name"], "display_name": _mailbox_display_name(r["mailbox_name"])}
        for r in raw_mailboxes
    ]
    return templates.TemplateResponse("index.html", {
        "request":        request,
        "projects":       projects,
        "mail_mailboxes": mail_mailboxes,
        "total_docs":     total_docs,
    })


@app.get("/search", response_class=HTMLResponse)
async def search(
    request:         Request,
    q:               str = Query(default=""),
    project_id:      str = Query(default=""),
    type:            str = Query(default=""),
    from_addr:       str = Query(default=""),
    to_addr:         str = Query(default=""),
    subject_filter:  str = Query(default=""),
    date_from:       str = Query(default=""),
    date_to:         str = Query(default=""),
    filesize:        str = Query(default=""),
    duplicates_only: str = Query(default=""),
    search_in:       str = Query(default=""),
    tag_id:          str = Query(default=""),
    scope:           str = Query(default=""),
    norms:           str = Query(default=""),  # veraltet, siehe unten
    search_token:    str = Query(default=""),
):
    # "scope" (all|plans|norms) ersetzt die frueheren getrennten Parameter "plans" (als
    # search_in-Wert) und "norms" (only/exclude) durch ein einziges, sich gegenseitig
    # ausschliessendes Segmented Control -- ein Dokument ist nie gleichzeitig "nur Plan"
    # und "nur Norm". Alte Links bleiben funktionsfaehig: norms=only -> scope=norms,
    # norms=exclude entfaellt ersatzlos (kein "Ohne Normen"-Zustand mehr vorgesehen).
    if not scope and norms == "only":
        scope = "norms"

    # "search_in" ist jetzt Einfachauswahl (wie "Umfang"): "" = Alle drei Bereiche,
    # sonst genau einer davon. Alte Komma-Listen-Links (z.B. "docs,filenames") bleiben
    # kompatibel, da set(...) auch bei mehreren Werten korrekt aufloest.
    in_scope = set(search_in.split(",")) if search_in else {"docs", "folders", "filenames"}
    filter_plans     = scope == "plans"
    search_docs      = "docs" in in_scope
    search_filenames = "filenames" in in_scope
    search_folders   = "folders" in in_scope

    results, error, total = [], None, 0
    folder_results = []
    search_log_id = None
    diagnose = None          # nur gesetzt, wenn eine Suche lief und nichts fand
    has_filters = any([from_addr, to_addr, subject_filter, date_from, date_to, filesize, duplicates_only, tag_id, scope, search_in])
    if q.strip() or has_filters:
        # run_in_executor: _run_scoped_search ist eine blockierende SQLite-Anfrage, die bei
        # Mehrwort-Queries mit vielen FTS-OR-Zweigen auf grossen Indizes mehrere Sekunden
        # dauern kann. Direkt im async-Handler wuerde das den einzigen uvicorn-Worker/
        # Event-Loop komplett blockieren -- ALLE anderen gleichzeitigen Anfragen (auch von
        # anderen Nutzern, auch MCP-Tool-Aufrufe wie list_folder) haengen so lange fest,
        # was sich als scheinbar zufaellige Timeouts aeussert.
        import asyncio

        def _do_search():
            conn = connection.get_connection()
            try:
                filters_str, filter_params = _build_filters(
                    project_id, type, from_addr, to_addr, subject_filter,
                    date_from, date_to, filesize, duplicates_only,
                    filter_plans=filter_plans, tag_id=tag_id,
                    norms="only" if scope == "norms" else "",
                )
                results, error = _run_scoped_search(conn, q.strip(), filters_str, filter_params,
                                                     in_scope)
                folders = _search_folders(conn, q.strip(), project_id) if search_folders and q.strip() else []
                _prefer_project_path(conn, results, project_id)
                # Nur wenn nichts gefunden wurde: nachsehen, ob ein einzelner Begriff
                # schuld ist. Läuft in diesem Thread mit, nicht im Event-Loop.
                diagnose = None
                if not results and not folders and not error:
                    diagnose = _leertreffer_diagnose(conn, q.strip(), filters_str,
                                                     filter_params, in_scope)
                return results, error, folders, diagnose
            finally:
                conn.close()

        loop = asyncio.get_event_loop()
        t0 = time.perf_counter()
        results, error, folder_results, diagnose = await loop.run_in_executor(None, _do_search)
        duration_ms = int((time.perf_counter() - t0) * 1000)
        total = len(results)

        from scanner.search_log import log_search
        log_conn = connection.get_connection()
        try:
            search_log_id = log_search(
                log_conn, "suche", q.strip(),
                project_id=int(project_id) if project_id.isdigit() else None,
                filters=_search_filters_summary(type, scope, duplicates_only, date_from, date_to, search_in),
                result_count=total + len(folder_results),
                duration_ms=duration_ms,
                token=search_token or None,
                query_string=str(request.query_params),
            )
        finally:
            log_conn.close()
    return templates.TemplateResponse("search_results.html", {
        "request":        request,
        "results":        results,
        "query":          q,
        "total":          total,
        "error":          error,
        "folder_results": folder_results,
        "search_log_id":  search_log_id,
        "diagnose":       diagnose,
    })



@app.post("/search-log/{search_log_id}/click")
async def search_log_click(search_log_id: int):
    """Fire-and-forget-Zähler: ein Suchergebnis wurde geöffnet/im Finder gezeigt.
    Wird per JS (trackSearchClick(), base.html) beim Klick auf 'Öffnen'/'Im Finder
    zeigen'/'In Apple Mail öffnen' ausgelöst -- siehe scanner/search_log.py."""
    from scanner.search_log import log_click

    conn = connection.get_connection()
    try:
        log_click(conn, search_log_id)
    finally:
        conn.close()
    return JSONResponse({"ok": True})


@app.get("/norms", response_class=HTMLResponse)
async def norms_page(request: Request):
    # Eine abgeschlossene "Aktualität aller Normen prüfen"-Meldung ("✓ Geprüft: X/X")
    # soll nur so lange sichtbar bleiben, wie man auf der Seite bleibt -- ein frischer
    # Aufruf dieser Route (Seitenwechsel/Neuladen) räumt sie weg. Läuft der Abgleich
    # noch ("running"), bleibt der Status unangetastet, damit der Fortschritt beim
    # Neuladen weiterhin sichtbar ist.
    if _norm_check_all["status"] in ("done", "offline"):
        _norm_check_all.update({"status": "idle", "done": 0, "total": 0, "list_refreshed": False})

    conn = connection.get_connection()
    proposed = conn.execute(
        "SELECT path, n_docs, n_norms, detected_at FROM norm_folders "
        "WHERE status = 'proposed' ORDER BY detected_at DESC"
    ).fetchall()
    confirmed = conn.execute(
        "SELECT path, n_docs, n_norms, decided_at FROM norm_folders "
        "WHERE status = 'confirmed' ORDER BY path"
    ).fetchall()
    rejected = conn.execute(
        "SELECT path, n_docs, n_norms, decided_at FROM norm_folders "
        "WHERE status = 'rejected' ORDER BY path"
    ).fetchall()
    total_norms = conn.execute("SELECT COUNT(*) FROM documents WHERE is_norm = 1").fetchone()[0]
    conn.close()
    return templates.TemplateResponse("norms.html", {
        "request":     request,
        "proposed":    [dict(r) for r in proposed],
        "confirmed":   [dict(r) for r in confirmed],
        "rejected":    [dict(r) for r in rejected],
        "total_norms": total_norms,
    })


@app.post("/norms/confirm", response_class=HTMLResponse)
async def norms_confirm(request: Request, path: str = Form(...)):
    from scanner.norms import reload_classifier
    from scanner.norms_learn import confirm_norm_folder

    conn = connection.get_connection()
    confirm_norm_folder(conn, path)
    reload_classifier(conn)
    conn.close()
    return await norms_page(request)


@app.post("/norms/reject", response_class=HTMLResponse)
async def norms_reject(request: Request, path: str = Form(...)):
    from scanner.norms import reload_classifier
    from scanner.norms_learn import reject_norm_folder

    conn = connection.get_connection()
    reject_norm_folder(conn, path)
    reload_classifier(conn)
    conn.close()
    return await norms_page(request)


@app.post("/norms/add-folder", response_class=HTMLResponse)
async def norms_add_folder(request: Request, path: str = Form(...)):
    """Manuell einen Ordner als Norm-Ordner eintragen -- für Ordner mit nur wenigen
    echten Normen, die den automatischen Vorschlags-Schwellwert nie erreichen und
    deshalb nie unter 'Ordner-Vorschläge' auftauchen."""
    from scanner.norms import reload_classifier
    from scanner.norms_learn import add_manual_norm_folder

    path = path.strip().rstrip("/")
    if path:
        conn = connection.get_connection()
        add_manual_norm_folder(conn, path)
        reload_classifier(conn)
        conn.close()
    return await norms_page(request)


@app.post("/norms/unmark/{doc_id}", response_class=HTMLResponse)
async def norms_unmark(doc_id: int):
    """Entfernt eine EINZELNE fälschlich erkannte Norm-Markierung -- anders als eine
    generelle Lockerung der Klassifikation (die neue False Positives andernorts
    riskieren würde) korrigiert das gezielt genau dieses eine Dokument, dauerhaft
    (norm_manual=1 verhindert, dass ein künftiger Scan es erneut markiert, siehe
    scanner/walker.py::_classify_norm). Gibt bewusst leeren Inhalt zurück -- die
    Zeile verschwindet aus der Liste, statt (fälschlich) weiter als Norm angezeigt
    zu werden."""
    conn = connection.get_connection()
    with conn:
        conn.execute(
            "UPDATE documents SET is_norm = 0, norm_manual = 1 WHERE id = ?", (doc_id,)
        )
    conn.close()
    return HTMLResponse("")


@app.post("/norms/reclassify", response_class=HTMLResponse)
async def norms_reclassify(request: Request):
    """Klassifiziert den kompletten Bestand anhand des bereits gespeicherten Volltexts
    neu (siehe scripts/backfill_norms.py) -- als Button direkt im UI, damit man dafür
    nicht ins Terminal muss. Notwendig nach jedem Rescan eines Ordners, der schon VOR
    der Norm-Erkennung indexiert war: ein normaler Scan überspringt unveränderte
    Dateien komplett und klassifiziert sie deshalb nie (scanner/walker.py, Schnellpfad
    in _process_file). Holt bei dieser Gelegenheit auch das Gültigkeitsdatum
    (norm_valid_from) für Normen nach, die schon vor dieser Funktion erkannt
    wurden und deshalb noch keins haben -- ein normaler Scan überspringt
    unveränderte Dateien ja ebenso und würde es sonst nie nachtragen."""
    from scanner.norms import extract_valid_from, get_classifier

    conn = connection.get_connection()
    try:
        classifier = get_classifier(conn)
        if classifier.enabled:
            rows = conn.execute("""
                SELECT d.id AS id, d.is_norm AS is_norm, dp.path AS path,
                       COALESCE(c.content, '') AS content, d.norm_valid_from AS norm_valid_from
                FROM documents d
                JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
                LEFT JOIN document_content c ON c.document_id = d.id
                WHERE d.norm_manual = 0
            """).fetchall()
            for row in rows:
                verdict = classifier.classify(row["path"], row["content"])
                new_is_norm = 1 if verdict.is_norm else 0
                if new_is_norm != row["is_norm"]:
                    conn.execute(
                        "UPDATE documents SET is_norm = ?, norm_reason = ? WHERE id = ?",
                        (new_is_norm, verdict.reason, row["id"]),
                    )
                if new_is_norm and not row["norm_valid_from"]:
                    valid_from = extract_valid_from(row["content"])
                    if valid_from:
                        conn.execute(
                            "UPDATE documents SET norm_valid_from = ? WHERE id = ?",
                            (valid_from, row["id"]),
                        )
            conn.commit()
    finally:
        conn.close()
    return await norms_page(request)


_NORMS_PAGE_SIZE = 50


_CHECK_STATUS_LABELS = {"aktuell": "Aktuell", "veraltet": "Veraltet", "ungeprüft": "Ungeprüft"}


def _norm_doc_dict(r) -> dict:
    from scanner.norms import guess_norm_number, guess_norm_type

    return {
        "id":            r["id"],
        "filename":      r["filename"],
        "path":          r["path"],
        "type":          guess_norm_type(r["filename"], r["content"]),
        "number":        guess_norm_number(r["filename"], r["content"]),
        "valid_from":    r["norm_valid_from"],
        "check_status":  r["norm_check_status"] or "ungeprüft",
        "check_label":   _CHECK_STATUS_LABELS.get(r["norm_check_status"] or "ungeprüft", "Ungeprüft"),
        "checked_at":    r["norm_checked_at"],
    }


@app.get("/norms/list", response_class=HTMLResponse)
async def norms_list(request: Request, offset: int = Query(default=0)):
    conn = connection.get_connection()
    rows = conn.execute("""
        SELECT d.id AS id, d.filename AS filename, dp.path AS path,
               COALESCE(c.content, '') AS content,
               d.norm_valid_from AS norm_valid_from,
               d.norm_check_status AS norm_check_status,
               d.norm_checked_at AS norm_checked_at
        FROM documents d
        JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        LEFT JOIN document_content c ON c.document_id = d.id
        WHERE d.is_norm = 1
        ORDER BY dp.path
        LIMIT ? OFFSET ?
    """, (_NORMS_PAGE_SIZE, offset)).fetchall()
    conn.close()
    docs = [_norm_doc_dict(r) for r in rows]
    return templates.TemplateResponse("_norms_list.html", {
        "request":     request,
        "docs":        docs,
        "has_more":    len(rows) == _NORMS_PAGE_SIZE,
        "next_offset": offset + _NORMS_PAGE_SIZE,
        "is_empty":    offset == 0 and not rows,
    })


def _ensure_norm_valid_from(conn, row, allow_reextract: bool = True) -> tuple[str | None, str]:
    """Liefert (norm_valid_from, content) -- extrahiert und speichert das Datum
    aber vorher, wenn es noch fehlt, sonst bräuchte der Online-Abgleich zwingend
    den separaten "Alle Dokumente nochmals auf Normen prüfen"-Lauf VORHER (leicht
    zu vergessen, und ohne Jahr bricht check_sia() sofort mit "ungeprüft" ab, ohne
    überhaupt einen Request zu versuchen -- sah dann aus wie ein hängender/
    fehlerhafter Abgleich, obwohl nur das Datum fehlte).

    Findet sich im bereits gespeicherten Volltext nichts UND allow_reextract ist
    True, wird EINMALIG frisch von der Datei neu extrahiert (derselbe
    Extraktions-/OCR-Pfad wie ein normaler Scan, siehe
    scanner.walker._extract_and_store -- komplett lokal, kein Netzwerk) -- der
    gespeicherte Text kann veraltet sein (Datei seit dem letzten Scan
    ausgetauscht, z.B. eine neue Norm-Ausgabe) oder von schwacher OCR-Qualität,
    ohne dass sich das über den Datei-Hash unterscheiden liesse, solange der Scan
    schlicht noch nicht erneut gelaufen ist. content wird zurückgegeben, damit der
    Aufrufer Typ/Nummer ebenfalls mit dem frischen Text statt dem alten bestimmt.

    allow_reextract=False beim Sammel-Lauf (_run_check_all): eine Neuextraktion
    kann pro Datei mehrere Sekunden dauern (OCR) -- bei 300+ Normen würde das den
    ohnehin schon langen Lauf unnötig weiter verlangsamen, wenn es nur um den
    einzelnen manuellen Button-Klick auf EINE Norm gehen sollte."""
    if row["norm_valid_from"]:
        return row["norm_valid_from"], row["content"]
    from scanner.norms import extract_valid_from

    valid_from = extract_valid_from(row["content"])
    if valid_from:
        with conn:
            conn.execute(
                "UPDATE documents SET norm_valid_from = ? WHERE id = ?",
                (valid_from, row["id"]),
            )
        return valid_from, row["content"]

    if allow_reextract and row["path"] and Path(row["path"]).exists():
        from scanner.walker import _extract_and_store
        try:
            _extract_and_store(conn, row["id"], Path(row["path"]))
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Frische Neuextraktion für Norm-Check fehlgeschlagen (Dokument %s): %s", row["id"], exc
            )
            return None, row["content"]
        updated = conn.execute(
            "SELECT norm_valid_from, COALESCE((SELECT content FROM document_content "
            "WHERE document_id = documents.id), '') AS content FROM documents WHERE id = ?",
            (row["id"],),
        ).fetchone()
        if updated:
            return updated["norm_valid_from"], updated["content"]
    return None, row["content"]


@app.post("/norms/check/{doc_id}", response_class=HTMLResponse)
async def norms_check_one(request: Request, doc_id: int):
    """Stösst den Online-Abgleich für EINE Norm an (Button in der Zeile) -- siehe
    scanner/norm_freshness.py. Läuft synchron (ein einzelner Request an SIA-Shop
    oder VSS-Shop dauert typischerweise unter einer Sekunde bis wenige Sekunden)."""
    from datetime import datetime, timezone

    from scanner.norm_freshness import check_norm
    from scanner.norms import guess_norm_number, guess_norm_type

    conn = connection.get_connection()
    row = conn.execute("""
        SELECT d.id AS id, d.filename AS filename, dp.path AS path,
               COALESCE(c.content, '') AS content, d.norm_valid_from AS norm_valid_from
        FROM documents d
        JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        LEFT JOIN document_content c ON c.document_id = d.id
        WHERE d.id = ? AND d.is_norm = 1
    """, (doc_id,)).fetchone()
    if not row:
        conn.close()
        return HTMLResponse("Norm nicht gefunden", status_code=404)

    valid_from, content = _ensure_norm_valid_from(conn, row)
    number = guess_norm_number(row["filename"], content)
    source = guess_norm_type(row["filename"], content)
    status, _detail = check_norm(number, valid_from, source)
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        conn.execute(
            "UPDATE documents SET norm_check_status = ?, norm_checked_at = ? WHERE id = ?",
            (status, checked_at, doc_id),
        )
    updated = conn.execute("""
        SELECT d.id AS id, d.filename AS filename, dp.path AS path,
               COALESCE(c.content, '') AS content,
               d.norm_valid_from AS norm_valid_from,
               d.norm_check_status AS norm_check_status,
               d.norm_checked_at AS norm_checked_at
        FROM documents d
        JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        LEFT JOIN document_content c ON c.document_id = d.id
        WHERE d.id = ?
    """, (doc_id,)).fetchone()
    conn.close()
    return templates.TemplateResponse("_norms_row.html", {
        "request": request, "d": _norm_doc_dict(updated),
    })


_norm_check_all = {"status": "idle", "done": 0, "total": 0, "list_refreshed": False}

# Nach dieser Frist wird eine bereits bestätigte Norm ('aktuell'/'veraltet') bei
# einem neuen Sammel-Lauf trotzdem nochmals geprüft -- sonst würde eine
# zwischenzeitlich abgelöste Norm für immer als "Aktuell" stehen bleiben, nur
# weil sie einmal bestätigt wurde. Innerhalb der Frist bleibt sie übersprungen
# (siehe _run_check_all-Docstring: das ist, was einen Wiederanlauf nach einem
# Abbruch schnell macht, statt wieder bei Norm 1 zu beginnen).
_RECHECK_AFTER_DAYS = 30


def _run_check_all():
    """Läuft über Normen mit Status 'ungeprüft' (Default, siehe Migration 020 --
    umfasst sowohl nie geprüfte als auch zuvor ergebnislos geprüfte Normen) SOWIE
    über bereits bestätigte Normen, deren letzte Prüfung länger als
    _RECHECK_AFTER_DAYS zurückliegt. Ein Abbruch/Absturz mitten im ca.
    30-minütigen Gesamtlauf bedeutet dadurch NICHT, dass ein Neustart wieder bei
    Norm 1 beginnt -- frisch bestätigte Normen werden übersprungen, ein erneuter
    Lauf kurz danach prüft nur noch den Rest. Nach _RECHECK_AFTER_DAYS werden
    auch bestätigte Normen wieder einbezogen, damit eine zwischenzeitlich
    abgelöste Norm nicht für immer als "Aktuell" stehen bleibt."""
    from datetime import datetime, timedelta, timezone

    from scanner.norm_freshness import OFFLINE_DETAIL, check_norm
    from scanner.norms import guess_norm_number, guess_norm_type

    stale_before = (datetime.now(timezone.utc) - timedelta(days=_RECHECK_AFTER_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = connection.get_connection()
    try:
        rows = conn.execute("""
            SELECT d.id AS id, d.filename AS filename, dp.path AS path,
                   COALESCE(c.content, '') AS content, d.norm_valid_from AS norm_valid_from
            FROM documents d
            JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
            LEFT JOIN document_content c ON c.document_id = d.id
            WHERE d.is_norm = 1 AND (
                d.norm_check_status = 'ungeprüft'
                OR d.norm_checked_at IS NULL
                OR d.norm_checked_at < ?
            )
        """, (stale_before,)).fetchall()
        _norm_check_all["total"] = len(rows)
        _norm_check_all["done"] = 0
        consecutive_offline = 0
        for row in rows:
            if _norm_check_all["status"] != "running":
                break  # abgebrochen
            valid_from, _content = _ensure_norm_valid_from(conn, row, allow_reextract=False)
            number = guess_norm_number(row["filename"], row["content"])
            source = guess_norm_type(row["filename"], row["content"])
            status, detail = check_norm(number, valid_from, source)
            checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with conn:
                conn.execute(
                    "UPDATE documents SET norm_check_status = ?, norm_checked_at = ? WHERE id = ?",
                    (status, checked_at, row["id"]),
                )
            _norm_check_all["done"] += 1
            # Archivio läuft bewusst auch komplett offline (Projektprinzip) -- ohne
            # diesen Abbruch würde der Lauf sich bei fehlendem Internetzugang durch
            # ALLE verbleibenden Normen quälen, obwohl jede einzelne genauso
            # scheitern würde. Erst nach 2 FOLGE-Ausfällen abbrechen, nicht schon
            # beim ersten -- ein einzelner Ausfall kann auch nur diese eine Norm
            # betreffen (z.B. ein kurzzeitig überlasteter Shop).
            if detail == OFFLINE_DETAIL:
                consecutive_offline += 1
                if consecutive_offline >= 2:
                    _norm_check_all["status"] = "offline"
                    break
            else:
                consecutive_offline = 0
            time.sleep(0.3)  # Höflichkeitspause zwischen Requests an fremde Shops
    finally:
        conn.close()
        if _norm_check_all["status"] == "running":
            _norm_check_all["status"] = "done"


@app.post("/norms/check-all")
async def norms_check_all_start():
    if _norm_check_all["status"] == "running":
        return JSONResponse({"ok": False, "error": "läuft bereits"})
    _norm_check_all.update({"status": "running", "done": 0, "total": 0, "list_refreshed": False})
    threading.Thread(target=_run_check_all, daemon=True).start()
    return JSONResponse({"ok": True})


@app.post("/norms/check-all/cancel")
async def norms_check_all_cancel():
    if _norm_check_all["status"] == "running":
        _norm_check_all["status"] = "cancelled"
    return JSONResponse({"ok": True})


@app.get("/norms/check-all/progress", response_class=HTMLResponse)
async def norms_check_all_progress(request: Request):
    # Die Normenliste soll sich EINMAL automatisch aktualisieren, sobald ein Lauf
    # fertig ist -- nicht bei jedem weiteren Poll (alle 2s), solange der Banner
    # "fertig" noch sichtbar ist. list_refreshed merkt sich das.
    auto_refresh = _norm_check_all["status"] in ("done", "offline") and not _norm_check_all["list_refreshed"]
    if auto_refresh:
        _norm_check_all["list_refreshed"] = True
    return templates.TemplateResponse("_norms_check_progress.html", {
        "request": request, "state": dict(_norm_check_all), "auto_refresh": auto_refresh,
    })


@app.get("/norms/search-candidates", response_class=HTMLResponse)
async def norms_search_candidates(request: Request, q: str = Query(default="")):
    """Dokumente zum manuellen Hinzufügen als Norm -- nur Nicht-Normen, per Datei-
    oder Pfadname gefiltert (kein Volltext -- hier geht es ums Finden eines konkreten
    bekannten Dokuments, nicht um inhaltliche Suche)."""
    q = q.strip()
    if not q:
        return HTMLResponse("")
    conn = connection.get_connection()
    rows = conn.execute("""
        SELECT d.id AS id, d.filename AS filename, dp.path AS path
        FROM documents d
        JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        WHERE d.is_norm = 0 AND (d.filename LIKE ? OR dp.path LIKE ?)
        ORDER BY d.filename
        LIMIT 20
    """, (f"%{q}%", f"%{q}%")).fetchall()
    conn.close()
    return templates.TemplateResponse("_norms_candidates.html", {
        "request": request,
        "docs":    [dict(r) for r in rows],
    })


# ── MCP-Protokoll ("Claude-Zugriffe") ───────────────────────────────────────────

def _mcp_log_query(project_id: str, status: str, date_from: str, date_to: str):
    """Baut WHERE-Klausel + Parameter für /mcp-log und den CSV-Export gemeinsam,
    damit Filter und Export nie auseinanderlaufen können."""
    where, params = [], []
    if project_id:
        where.append("project_id = ?")
        params.append(int(project_id))
    if status:
        where.append("status = ?")
        params.append(status)
    if date_from:
        where.append("ts >= ?")
        params.append(f"{date_from}T00:00:00Z")
    if date_to:
        where.append("ts <= ?")
        params.append(f"{date_to}T23:59:59Z")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return clause, params


def _group_mcp_log_entries(entries: list[dict]) -> list[dict]:
    """Fasst aufeinanderfolgende Zeilen (entries bereits nach ts DESC sortiert) mit
    derselben session_id zu einer Gruppe zusammen -- eine einzelne Nutzerfrage löst
    bei Claude oft mehrere Such-/Nachlade-Aufrufe aus (siehe helper/archivio_mcp.py
    _SESSION_ID, einmal pro Subprozess/Verbindung erzeugt), die sonst wie viele
    unabhängige Zugriffe wirken. Zeilen ohne session_id (vor Migration 018, oder
    ein manueller HTTP-Aufruf ohne den Parameter) bleiben bewusst einzeln -- kein
    Zusammenfassen über eine fehlende ID hinweg, sonst könnten fremde Zugriffe
    fälschlich in eine gemeinsame Gruppe rutschen."""
    groups: list[dict] = []
    current: dict | None = None
    for e in entries:
        sid = e.get("session_id")
        if sid and current is not None and current["session_id"] == sid:
            current["entries"].append(e)
        else:
            current = {"session_id": sid, "entries": [e]}
            groups.append(current)
    for g in groups:
        es = g["entries"]
        g["count"]         = len(es)
        g["files_count"]   = sum(len(e["files"]) for e in es)
        g["blocked_count"] = sum(len(e["blocked"]) for e in es)
        g["ts_start"]      = es[-1]["ts"]  # es ist DESC sortiert -> letztes Element = frühester Zeitpunkt
        g["ts_end"]        = es[0]["ts"]
        g["projects"]      = sorted({e["project_name"] for e in es if e.get("project_name")})
        statuses = {e["status"] for e in es}
        g["status"] = "error" if "error" in statuses else ("blocked" if "blocked" in statuses else "ok")
    return groups


def _group_scan_log_entries(scans: list[dict]) -> list[dict]:
    """Fasst alle Zeilen mit derselben batch_id (siehe web/api.py::scan_all(),
    Migration 019) zu einer Gruppe zusammen -- ein "Alle scannen"-Lauf (Klick oder
    nächtlicher Scheduler) erzeugt sonst pro Projekt eine eigene, unzusammenhängende
    Zeile im Systemstatus. Anders als bei _group_mcp_log_entries() wird hier NICHT
    nur bei direkt aufeinanderfolgenden Zeilen zusammengefasst, sondern über die
    gesamte Liste hinweg -- batch_id kommt ausschliesslich von einem echten,
    gemeinsamen scan_all()-Aufruf, ein Vermischen fremder Zeilen ist dadurch
    ausgeschlossen. Zeilen ohne batch_id (Einzel-Scan eines Projekts) bleiben
    einzeln."""
    groups: list[dict] = []
    by_batch: dict[str, dict] = {}
    for s in scans:
        bid = s.get("batch_id")
        if not bid:
            groups.append({"batch_id": None, "entries": [s]})
            continue
        if bid in by_batch:
            by_batch[bid]["entries"].append(s)
        else:
            g = {"batch_id": bid, "entries": [s]}
            by_batch[bid] = g
            groups.append(g)
    for g in groups:
        es = g["entries"]
        g["count"]          = len(es)
        g["total_files"]    = sum(e["total"] or 0 for e in es)
        g["total_new"]      = sum(e["new_count"] or 0 for e in es)
        g["total_errors"]   = sum(e["error_count"] or 0 for e in es)
        g["total_duration_s"] = sum(e.get("duration_s") or 0 for e in es)
        g["ts_start"]       = min(e["started_at"] for e in es)
        g["ts_end"]         = max((e["finished_at"] for e in es if e.get("finished_at")), default=None)
        g["peak_memory_mb"] = max((e["peak_memory_mb"] or 0) for e in es)
        g["peak_cpu_pct"]   = max((e["peak_cpu_pct"] or 0) for e in es)
        statuses = {e["status"] for e in es}
        g["status"] = "error" if "error" in statuses else ("cancelled" if "cancelled" in statuses else "done")
    return groups


@app.get("/mcp-log", response_class=HTMLResponse)
async def mcp_log_page(
    request:    Request,
    project_id: str = Query(default=""),
    status:     str = Query(default=""),
    date_from:  str = Query(default=""),
    date_to:    str = Query(default=""),
):
    import json as _json
    from datetime import timedelta

    conn = connection.get_connection()
    clause, params = _mcp_log_query(project_id, status, date_from, date_to)
    rows = conn.execute(
        f"SELECT * FROM mcp_log {clause} ORDER BY ts DESC LIMIT 500", params
    ).fetchall()
    projects   = conn.execute("SELECT id, name FROM projects ORDER BY name").fetchall()
    proj_names = {p["id"]: p["name"] for p in projects}

    entries = []
    for r in rows:
        d = dict(r)
        d["files"]        = _json.loads(d["files_json"] or "[]")
        d["blocked"]      = _json.loads(d["blocked_json"] or "[]")
        d["project_name"] = proj_names.get(d["project_id"])
        entries.append(d)

    since  = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = conn.execute(
        "SELECT files_json FROM mcp_log WHERE ts >= ?", (since,)
    ).fetchall()

    from web.dashboard import _block_rules_context
    block_ctx = _block_rules_context(conn)
    conn.close()

    groups = _group_mcp_log_entries(entries)

    return templates.TemplateResponse("mcp_log.html", {
        "request":           request,
        "entries":           entries,
        "groups":            groups,
        "projects":          [dict(p) for p in projects],
        "filter_project_id": project_id,
        "filter_status":     status,
        "filter_from":       date_from,
        "filter_to":         date_to,
        "total_30d":         len(recent),
        "files_30d":         sum(len(_json.loads(r["files_json"] or "[]")) for r in recent),
        **block_ctx,
    })


@app.get("/mcp-log/export.csv")
async def mcp_log_export(
    project_id: str = Query(default=""),
    status:     str = Query(default=""),
    date_from:  str = Query(default=""),
    date_to:    str = Query(default=""),
):
    import csv
    import io
    import json as _json

    conn = connection.get_connection()
    clause, params = _mcp_log_query(project_id, status, date_from, date_to)
    rows = conn.execute(
        f"SELECT * FROM mcp_log {clause} ORDER BY ts DESC LIMIT 5000", params
    ).fetchall()
    projects   = conn.execute("SELECT id, name FROM projects").fetchall()
    proj_names = {p["id"]: p["name"] for p in projects}
    conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Zeitstempel", "Tool", "Anfrage", "Projekt", "Status",
                      "Übermittelte Dateien", "Zeichen", "Blockiert (Grund)"])
    for r in rows:
        files   = _json.loads(r["files_json"] or "[]")
        blocked = _json.loads(r["blocked_json"] or "[]")
        writer.writerow([
            r["ts"], r["tool"], r["query"] or "",
            proj_names.get(r["project_id"], ""), r["status"],
            "; ".join(f.get("filename") or f.get("path") or "" for f in files),
            r["chars_sent"],
            "; ".join(
                f"{b.get('filename') or b.get('path')} ({b.get('reason')})"
                if (b.get("filename") or b.get("path")) else (b.get("reason") or "")
                for b in blocked
            ),
        ])
    return HTMLResponse(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=claude-zugriffe.csv"},
    )


@app.get("/search-log/export.csv")
async def search_log_export():
    """CSV-Export des kompletten Suche-Protokolls (siehe scanner/search_log.py) --
    dient als Rohdaten-Grundlage, um die Suche später anhand echter Anfragen zu
    verbessern (z.B. häufige Anfragen ohne Treffer oder ohne Klick), ausserhalb
    von Archivio selbst auszuwerten (Excel/Numbers). Ohne Filter-Parameter --
    anders als /mcp-log hat die Systemstatus-Seite keine Filterleiste dafür,
    ein kompletter Export reicht für diesen Zweck."""
    import csv
    import io

    conn = connection.get_connection()
    rows = conn.execute("SELECT * FROM search_log ORDER BY ts DESC LIMIT 20000").fetchall()
    proj_names = {p["id"]: p["name"] for p in conn.execute("SELECT id, name FROM projects")}
    conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Zeitstempel", "Art", "Anfrage", "Projekt", "Filter",
                      "Treffer", "Dauer (ms)", "Klicks"])
    for r in rows:
        writer.writerow([
            r["ts"], "Suche",
            r["query"] or "", proj_names.get(r["project_id"], ""),
            r["filters"] or "", r["result_count"], r["duration_ms"], r["clicks"],
        ])
    return HTMLResponse(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=suche-protokoll.csv"},
    )


@app.get("/system-status", response_class=HTMLResponse)
async def system_status_page(request: Request):
    import json as _json
    from datetime import timedelta

    import psutil

    conn = connection.get_connection()
    rows = conn.execute(
        "SELECT * FROM scan_log ORDER BY started_at DESC LIMIT 200"
    ).fetchall()
    scans = []
    for r in rows:
        d = dict(r)
        d["errors"] = _json.loads(d["errors_json"] or "[]")
        scans.append(d)
    groups = _group_scan_log_entries(scans)

    since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    search_rows = conn.execute(
        "SELECT * FROM search_log ORDER BY ts DESC LIMIT 300"
    ).fetchall()
    proj_names = {p["id"]: p["name"] for p in conn.execute("SELECT id, name FROM projects")}
    search_entries = []
    for r in search_rows:
        d = dict(r)
        d["project_name"] = proj_names.get(d["project_id"])
        search_entries.append(d)
    search_recent = conn.execute(
        "SELECT result_count, duration_ms FROM search_log WHERE ts >= ?", (since,)
    ).fetchall()
    conn.close()

    search_total_30d = len(search_recent)
    search_avg_ms = (
        sum(r["duration_ms"] or 0 for r in search_recent) / search_total_30d if search_total_30d else 0
    )
    search_empty_30d = sum(1 for r in search_recent if r["result_count"] == 0)
    # Nur ein einzelner, guenstiger Aufruf pro Seitenaufruf -- kein Sampling waehrend
    # des Scans selbst, bremst also nichts (siehe scanner/scan_log.py::ResourceSampler,
    # das misst weiterhin nur RSS/CPU, keine zusaetzliche RAM-Erfassung dort noetig).
    total_ram_mb = psutil.virtual_memory().total / (1024 * 1024)

    from db import backup as backup_mod
    return templates.TemplateResponse("system_status.html", {
        "request":          request,
        "backup":           backup_mod.summary(),
        "scans":            scans,
        "groups":           groups,
        "total_ram_mb":     total_ram_mb,
        "search_entries":   search_entries,
        "search_total_30d": search_total_30d,
        "search_avg_ms":    search_avg_ms,
        "search_empty_30d": search_empty_30d,
    })


@app.get("/open")
async def open_file(path: str = Query(...)):
    try:
        result = subprocess.run(["open", path], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return JSONResponse({"ok": False, "error": result.stderr or result.stdout}, status_code=500)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)



@app.get("/reveal")
async def reveal_file(path: str = Query(...)):
    try:
        subprocess.run(["open", "-R", path], check=True, timeout=5)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/open/mail/{document_id}")
async def open_mail(document_id: int):
    """Gibt message://-URL zurück — der Helper öffnet sie lokal auf dem Client-Mac."""
    conn = connection.get_connection()
    row  = conn.execute(
        "SELECT hash FROM documents WHERE id=? AND source_type='email'",
        (document_id,),
    ).fetchone()
    conn.close()
    if not row:
        return JSONResponse({"ok": False, "error": "Mail nicht gefunden"}, status_code=404)
    mid = row["hash"].strip("<>").strip()
    url = f"message://%3C{quote(mid, safe='')}%3E"
    return JSONResponse({"ok": True, "url": url})


@app.post("/api/open/mail/{document_id}")
async def open_mail_server(document_id: int):
    """Öffnet Mail direkt auf dem Server-Mac (Fallback wenn kein Helper läuft)."""
    conn = connection.get_connection()
    row  = conn.execute(
        "SELECT hash FROM documents WHERE id=? AND source_type='email'",
        (document_id,),
    ).fetchone()
    conn.close()
    if not row:
        return JSONResponse({"ok": False, "error": "Mail nicht gefunden"}, status_code=404)
    mid = row["hash"].strip("<>").strip()
    url = f"message://<{mid}>"
    try:
        subprocess.run(["open", url], timeout=5, check=False)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/preview/{document_id}", response_class=HTMLResponse)
async def preview(request: Request, document_id: int, q: str = Query(default="")):
    """Lazy-Vorschau für ein Suchergebnis."""
    conn = connection.get_connection()
    doc  = conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
    if not doc:
        conn.close()
        return HTMLResponse("")
    doc = dict(doc)
    content_row = conn.execute(
        "SELECT content FROM document_content WHERE document_id=?", (document_id,)
    ).fetchone()
    raw     = (content_row["content"] if content_row else "") or ""
    excerpt = raw[:600].rstrip() + ("…" if len(raw) > 600 else "")
    if q:
        for w in [re.sub(r'["\(\)\*\:\^]', "", x) for x in q.split() if x]:
            excerpt = re.sub(
                f"({re.escape(w)})", r"<mark>\1</mark>",
                excerpt, flags=re.IGNORECASE,
            )

    ctx: dict = {"request": request, "doc": doc, "excerpt": excerpt, "q": q}

    if doc["source_type"] == "email":
        mail = conn.execute(
            "SELECT * FROM mails WHERE document_id=?", (document_id,)
        ).fetchone()
        ctx["mail"] = dict(mail) if mail else {}

    conn.close()
    return templates.TemplateResponse("_preview.html", ctx)


@app.post("/toggle-norm/{document_id}", response_class=HTMLResponse)
async def toggle_norm(request: Request, document_id: int, q: str = Query(default="")):
    """Manuelle Norm-Markierung -- bewusst EINWEG (nur markieren, nie entfernen).
    'Im Zweifel sperren': eine einmal gesetzte Markierung bleibt auch bei einem
    Fehlalarm bestehen -- lokale Suche/Anzeige funktioniert unverändert weiter, nur
    der MCP/KI-Export bleibt für dieses Dokument dauerhaft gesperrt. norm_manual=1
    verhindert, dass ein Rescan (scanner/walker.py::_classify_norm) das je überschreibt."""
    conn = connection.get_connection()
    with conn:
        conn.execute(
            "UPDATE documents SET is_norm = 1, norm_manual = 1, norm_reason = 'manuell markiert' "
            "WHERE id = ?",
            (document_id,),
        )
    conn.close()
    return await preview(request, document_id, q)


# ── Such-Logik ────────────────────────────────────────────────────────────────

def _filename_score(filename: str, q: str) -> int:
    """Höherer Score = bessere Übereinstimmung mit Dateiname.
    Stufen pro Suchwort: exakter Stem-Treffer (100) > Stem beginnt mit Wort (40)
    > ganzes Token im Stem (20) > Substring irgendwo (5).
    """
    words = [re.sub(r'["\(\)\*\:\^]', "", w) for w in q.lower().split()]
    words = [w for w in words if w and w not in _STOPWORDS]
    if not words:
        return 0
    stem = re.sub(r'\.[^.]+$', '', filename.lower())  # extension entfernen
    fn   = filename.lower()
    score = 0
    for w in words:
        if stem == w:
            score += 100
        elif stem.startswith(w):
            score += 40
        elif re.search(r'(?<![a-z0-9À-ÿ])' + re.escape(w) + r'(?![a-z0-9À-ÿ])', stem):
            score += 20
        elif w in fn:
            score += 5
    return score

def _search(conn, q: str, filters: str, filter_params: list):
    if not q:
        return _search_filtered(conn, filters, filter_params)
    results, error = _search_fts(conn, q, filters, filter_params)
    if error:
        return results, error
    # Dateinamen-Treffer immer zusätzlich prüfen — chunks_fts enthält keine Dateinamen
    fname_results = _search_filename(conn, q, filters, filter_params)
    seen = {r["id"] for r in results}
    for r in fname_results:
        if r["id"] not in seen:
            seen.add(r["id"])  # sonst koennen Duplikate INNERHALB von fname_results durchrutschen
            results.append(r)
    # Filename-Treffer nach oben schieben
    results.sort(key=lambda r: _filename_score(r.get("filename", ""), q), reverse=True)
    if results:
        return results, None
    results, error = _search_like(conn, q, filters, filter_params)
    for r in results:
        r["fallback"] = True
    return results, error


def _run_scoped_search(conn, q: str, filters: str, filter_params: list, scope: set):
    """Führt die Dokument-/Dateinamen-Suche gemäß scope aus (Teilmenge von
    {"docs", "filenames"} -- weitere Werte wie "folders"/"plans" werden ignoriert, die
    behandelt der jeweilige Aufrufer separat). Gemeinsame Logik für die /search-Route
    und /api/mcp/search: vorher ignorierte der MCP-Endpoint search_in komplett und
    führte immer die volle Kombisuche aus, unabhängig vom angeforderten scope."""
    search_docs      = "docs" in scope
    search_filenames = "filenames" in scope
    if search_docs and search_filenames:
        return _search(conn, q, filters, filter_params)
    if search_docs:
        if q:
            results, error = _search_fts(conn, q, filters, filter_params)
            if not results and not error:
                results, error = _search_like(conn, q, filters, filter_params)
                for r in results:
                    r["fallback"] = True
            return results, error
        return _search_filtered(conn, filters, filter_params)
    if search_filenames:
        if q:
            results = _search_filename(conn, q, filters, filter_params)
            results.sort(key=lambda r: _filename_score(r.get("filename", ""), q), reverse=True)
            return results, None
        return _search_filtered(conn, filters, filter_params)
    return [], None


def _prefer_project_path(conn, results: list[dict], project_id: str) -> None:
    """Korrigiert Pfad/Projektname von Ergebnissen für die Anzeige, wenn ein Projektfilter
    aktiv ist: Dateiidentität läuft über SHA256-Hash, nicht Pfad — dieselbe physische Datei
    kann (z. B. als kopierte Vorlage) in mehreren Projektordnern liegen. Der Filter matcht
    dann zurecht (eine Kopie liegt wirklich im gefilterten Projekt), aber die Anzeige
    verwendet sonst immer den EINEN "primären" Pfad des Dokuments, der aus einem ganz
    anderen Projekt stammen kann — wirkt dann wie ein Filter-Leck, ist aber keins. Biegt
    Pfad + Projektname auf die Kopie um, die tatsächlich im gefilterten Projekt liegt.
    Mutiert `results` in place, kein Rückgabewert."""
    if not project_id or project_id.startswith("mailbox:"):
        return
    try:
        pid = int(project_id)
    except ValueError:
        return
    prow = conn.execute("SELECT path, name FROM projects WHERE id=?", (pid,)).fetchone()
    if not prow or not prow["path"] or str(prow["path"]).startswith("mailbox:"):
        return
    prefix = str(prow["path"]).rstrip("/") + "/"

    mismatched = [r for r in results if r.get("filepath") and not r["filepath"].startswith(prefix)]
    if not mismatched:
        return
    doc_ids = [r["id"] for r in mismatched]
    placeholders = ",".join("?" * len(doc_ids))
    rows = conn.execute(
        f"SELECT document_id, path FROM document_paths "
        f"WHERE document_id IN ({placeholders}) AND path LIKE ?",
        doc_ids + [prefix + "%"],
    ).fetchall()
    best_path: dict[int, str] = {}
    for row in rows:
        best_path.setdefault(row["document_id"], row["path"])  # erster Treffer reicht
    for r in mismatched:
        if r["id"] in best_path:
            r["filepath"]     = best_path[r["id"]]
            r["project_name"] = prow["name"]


_TYPE_CATEGORIES: dict[str, list[str]] = {
    "cat:bilder":    [".png", ".jpg", ".jpeg", ".tiff", ".tif", ".heic", ".heif",
                      ".webp", ".gif", ".bmp", ".svg", ".raw", ".cr2", ".nef", ".arw", ".dng"],
    "cat:cad":       [".dwg", ".dxf", ".pln", ".vwx", ".ifc", ".rvt", ".skp", ".3dm", ".nwd"],
    "cat:3d":        [".step", ".stp", ".stl", ".3ds", ".obj", ".fbx", ".c4d"],
    "cat:adobe":     [".psd", ".ai", ".indd"],
    "cat:video":     [".mp4", ".mov", ".avi", ".mkv", ".m4v", ".wmv",
                      ".mp3", ".wav", ".aac", ".m4a", ".flac"],
    "cat:dokumente": [".pdf", ".docx", ".doc", ".dotx", ".xlsx", ".xls", ".xlsm",
                      ".pptx", ".ppt", ".txt", ".rtf", ".csv", ".eml", ".msg"],
    # .dotx (Word-Vorlage) ist strukturell dasselbe OOXML-Format wie .docx -- gehoert
    # in denselben Filter, damit Vorlagen beim Filtern nach "Word (DOCX)" mit auftauchen.
    ".docx":         [".docx", ".dotx"],
}


def _search_filters_summary(
    ext: str, scope: str, duplicates_only: str, date_from: str, date_to: str, search_in: str,
) -> str:
    """Kompakte, menschenlesbare Zusammenfassung der gesetzten Filter für eine
    Protokollzeile im Suche-Protokoll (scanner/search_log.py) -- Projekt wird dort
    separat per project_id/project_name aufgelöst, taucht deshalb hier nicht auf."""
    parts = []
    if ext:
        parts.append(f"Typ: {ext}")
    if scope == "plans":
        parts.append("Nur Pläne")
    elif scope == "norms":
        parts.append("Nur Normen")
    if duplicates_only:
        parts.append("Nur Duplikate")
    if date_from or date_to:
        parts.append(f"Zeitraum {date_from or '…'}–{date_to or '…'}")
    if search_in and search_in != "docs,folders,filenames":
        parts.append(f"Bereich: {search_in}")
    return " · ".join(parts)


def _build_filters(
    project_id: str, ext: str,
    from_addr: str = "", to_addr: str = "", subject_filter: str = "",
    date_from: str = "", date_to: str = "",
    filesize: str = "", duplicates_only: str = "",
    filter_plans: bool = False, tag_id: str = "", norms: str = "",
) -> tuple[str, list]:
    filters, params = "", []
    if norms == "only":
        filters += " AND d.is_norm = 1"
    if tag_id:
        try:
            filters += " AND d.id IN (SELECT document_id FROM photo_tag_assignments WHERE tag_id = ?)"
            params.append(int(tag_id))
        except ValueError:
            pass
    if filter_plans:
        filters += " AND json_extract(d.metadata, '$.is_plan') = 1"
    if project_id:
        if project_id.startswith("mailbox:"):
            filters += " AND m.mailbox_name = ? AND d.project_id IS NULL"
            params.append(project_id[8:])
        else:
            try:
                # Eltern-Projekt und alle Sub-Projekte (Pfad-Prefix) einschliessen.
                # Zweite Bedingung: Dokumente die physisch im Sub-Projekt-Ordner liegen,
                # aber noch unter dem Eltern-Projekt indexiert sind (vor Sub-Projekt-Aktivierung).
                filters += (
                    " AND (d.project_id IN ("
                    "  SELECT p2.id FROM projects p2"
                    "  JOIN projects p1 ON (p2.path = p1.path OR p2.path LIKE (p1.path || '/%'))"
                    "  WHERE p1.id = ?"
                    ") OR d.id IN ("
                    "  SELECT dp2.document_id FROM document_paths dp2"
                    "  JOIN projects p3 ON dp2.path LIKE (p3.path || '/%')"
                    "  WHERE p3.id = ?"
                    "))"
                )
                params.extend([int(project_id), int(project_id)])
            except ValueError:
                pass
    if ext == "mail":
        filters += " AND d.source_type = 'email'"
    elif ext in _TYPE_CATEGORIES:
        exts = _TYPE_CATEGORIES[ext]
        placeholders = ",".join("?" * len(exts))
        filters += f" AND d.extension IN ({placeholders})"
        params.extend(exts)
    elif ext:
        e = ext if ext.startswith(".") else f".{ext}"
        filters += " AND d.extension = ?"
        params.append(e)
    if from_addr:
        filters += " AND m.sender LIKE ?"
        params.append(f"%{from_addr}%")
    if to_addr:
        filters += " AND (m.recipients LIKE ? OR m.cc LIKE ?)"
        params.extend([f"%{to_addr}%", f"%{to_addr}%"])
    if subject_filter:
        filters += " AND m.subject LIKE ?"
        params.append(f"%{subject_filter}%")
    if date_from:
        filters += " AND d.modified_at >= ?"
        params.append(date_from)
    if date_to:
        filters += " AND d.modified_at <= ?"
        params.append(date_to + "T23:59:59")
    if filesize:
        try:
            filters += " AND d.filesize > ?"
            params.append(int(filesize) * 1024 * 1024)
        except ValueError:
            pass
    if duplicates_only:
        filters += (" AND d.id IN ("
                    "SELECT document_id FROM document_paths "
                    "GROUP BY document_id HAVING COUNT(*) > 1)")
    return filters, params


def _search_filtered(conn, filters: str, filter_params: list):
    sql = f"""
        SELECT d.id, d.filename, d.extension, d.filesize, d.modified_at,
               d.extraction_status, d.source_type, d.is_norm,
               COALESCE(p.name, m.mailbox_name) AS project_name,
               dp.path     AS filepath,
               dc.content  AS raw_content,
               m.sender    AS mail_sender,
               m.date      AS mail_date
        FROM documents d
        LEFT JOIN projects        p  ON p.id  = d.project_id
        LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        LEFT JOIN document_content dc ON dc.document_id = d.id
        LEFT JOIN mails       m  ON m.document_id = d.id
        WHERE 1=1 {filters}
        ORDER BY d.modified_at DESC
        LIMIT 50
    """
    try:
        rows = conn.execute(sql, filter_params).fetchall()
        seen    = set()
        results = []
        for r in rows:
            d = dict(r)
            # Siehe _search_filename(): mehrfach "primaere" Pfade desselben Dokuments
            # koennen den LEFT JOIN sonst vervielfachen.
            if d["id"] in seen:
                continue
            seen.add(d["id"])
            d["fallback"]    = False
            d["page_number"] = None
            d["excerpt"]     = d.pop("raw_content") or ""
            results.append(d)
        return results, None
    except Exception as exc:
        return [], _suchfehler(exc, q)


def _search_fts(conn, q: str, filters: str, filter_params: list):
    fts_q = _make_fts_query(q)
    sql = f"""
        SELECT
            d.id, d.filename, d.extension, d.filesize, d.modified_at,
            d.extraction_status, d.source_type,
            COALESCE(p.name, m.mailbox_name) AS project_name,
            dp.path     AS filepath,
            dc_chunk.content AS raw_content,
            dc_chunk.page_number,
            chunks_fts.rank,
            m.sender    AS mail_sender,
            m.date      AS mail_date
        FROM chunks_fts
        JOIN  document_chunks dc_chunk ON chunks_fts.rowid = dc_chunk.id
        JOIN  documents       d        ON d.id = dc_chunk.document_id
        LEFT JOIN projects        p        ON p.id = d.project_id
        LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        LEFT JOIN mails       m  ON m.document_id = d.id
        WHERE chunks_fts MATCH ?
        {filters}
        ORDER BY rank
        LIMIT 200
    """
    try:
        rows    = conn.execute(sql, [fts_q] + filter_params).fetchall()
        seen    = set()
        results = []
        for r in rows:
            d = dict(r)
            if d["id"] in seen:
                continue
            seen.add(d["id"])
            d["fallback"] = False
            d["excerpt"]  = _excerpt(d.pop("raw_content") or "", q)
            d.pop("rank", None)
            results.append(d)
            if len(results) >= 50:
                break
        return results, None
    except Exception as exc:
        return [], _suchfehler(exc, q)


def _wort_kommt_vor(conn, wort: str, filters: str, filter_params: list) -> bool:
    """Gibt es überhaupt EIN Dokument mit diesem Wort (unter den aktiven Filtern)?

    Bewusst nur ein Existenztest mit LIMIT 1 statt einer vollständigen Suche: bei
    einem Wort wie „Eingabe" gäbe es tausende Treffer, gebraucht wird aber nur die
    Antwort ja/nein. Geprüft wird beides -- Volltext und Dateiname --, sonst würde ein
    Wort, das nur in Dateinamen vorkommt, fälschlich als unbekannt gemeldet."""
    volltext = f"""
        SELECT 1 FROM chunks_fts
        JOIN  document_chunks dc_chunk ON chunks_fts.rowid = dc_chunk.id
        JOIN  documents       d        ON d.id = dc_chunk.document_id
        LEFT JOIN projects       p  ON p.id = d.project_id
        LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        LEFT JOIN mails          m  ON m.document_id = d.id
        WHERE chunks_fts MATCH ? {filters} LIMIT 1
    """
    dateiname = f"""
        SELECT 1 FROM documents_fts
        JOIN  documents       d  ON d.id = documents_fts.rowid
        LEFT JOIN projects       p  ON p.id = d.project_id
        LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        LEFT JOIN mails          m  ON m.document_id = d.id
        WHERE documents_fts MATCH ? {filters} LIMIT 1
    """
    for sql, ausdruck in ((volltext, f"{wort}*"), (dateiname, f"filename:{wort}*")):
        try:
            if conn.execute(sql, [ausdruck] + filter_params).fetchone():
                return True
        except Exception as exc:
            logging.getLogger(__name__).warning("Existenzprüfung für %r: %s", wort, exc)
    return False


def _leertreffer_diagnose(conn, q: str, filters: str, filter_params: list,
                          in_scope) -> dict | None:
    """Erklärt, warum eine Mehrwort-Suche nichts gefunden hat.

    Mehrere Wörter werden mit UND verknüpft -- ein einziger Begriff, den es nirgends
    gibt, lässt die ganze Suche ins Leere laufen. Für den Nutzer sieht das aus wie
    „das Dokument existiert nicht", dabei ist meist nur ein Wort vertippt. Real
    beobachtet: Suche nach „260902 Afo Eingabe", während die Datei „260209 Afo
    Eingabe…" heisst -- zwei vertauschte Ziffern, null Treffer, kein Hinweis.

    Läuft ausschliesslich, wenn ohnehin nichts gefunden wurde; erfolgreiche Suchen
    kostet das nichts. Gibt None zurück, wenn jedes Wort für sich vorkommt -- dann
    liegt es nicht an einem einzelnen Begriff.
    """
    woerter = [w for w in _TRENNZEICHEN.split(q)
               if w and w.lower() not in _STOPWORDS]
    if len(woerter) < 2:
        return None

    fehlend = [w for w in woerter if not _wort_kommt_vor(conn, w, filters, filter_params)]

    if fehlend:
        rest = [w for w in woerter if w not in fehlend]
        if not rest:
            return {"art": "unbekannt", "fehlend": fehlend, "rest": [], "rest_treffer": 0}
        rest_query = " ".join(rest)
        treffer, _ = _run_scoped_search(conn, rest_query, filters, filter_params, in_scope)
        return {"art": "unbekannt", "fehlend": fehlend, "rest": rest,
                "rest_query": rest_query, "rest_treffer": len(treffer)}

    # Jedes Wort kommt vor, nur nie im selben Dokument. Dann hilft die Frage: welcher
    # eine Begriff engt so stark ein, dass nichts übrig bleibt? Beispiel aus der
    # Praxis: "treppenhaus hochhaus lift plan keller rietbachstrasse" -- ohne den
    # Strassennamen gibt es Treffer.
    #
    # Dafür wird pro Wort einmal ohne dieses Wort gesucht. Das sind bis zu sechs
    # zusätzliche Abfragen, aber nur auf dem Null-Treffer-Pfad; erfolgreiche Suchen
    # kostet es nichts. Ab sieben Wörtern lohnt der Aufwand nicht mehr -- dann ist
    # die Anfrage ohnehin eher ein Satz als eine Suche.
    if len(woerter) > 6:
        return None

    bester = None
    for w in woerter:
        rest = [x for x in woerter if x != w]
        rest_query = " ".join(rest)
        treffer, _ = _run_scoped_search(conn, rest_query, filters, filter_params, in_scope)
        if treffer and (bester is None or len(treffer) > bester["rest_treffer"]):
            bester = {"art": "zu_eng", "weglassen": [w], "rest": rest,
                      "rest_query": rest_query, "rest_treffer": len(treffer)}
    return bester


def _search_like(conn, q: str, filters: str, filter_params: list):
    # Gleiche Zerlegung wie die FTS-Suche: sonst sucht der LIKE-Zweig nach "u-wert"
    # als Ganzes, während im Index "u" und "wert" stehen.
    words = [w for w in _TRENNZEICHEN.split(q) if w]
    if not words:
        return [], None

    like_clauses = " AND ".join(
        "(COALESCE(dc.content,'') LIKE ? OR d.filename LIKE ?)"
        for _ in words
    )
    like_params = [p for w in words for p in (f"%{w}%", f"%{w}%")]

    sql = f"""
        SELECT
            d.id, d.filename, d.extension, d.filesize, d.modified_at,
            d.extraction_status, d.source_type,
            COALESCE(p.name, m.mailbox_name) AS project_name,
            dp.path     AS filepath,
            dc.content  AS raw_content,
            m.sender    AS mail_sender,
            m.date      AS mail_date
        FROM documents d
        LEFT JOIN projects        p  ON p.id  = d.project_id
        LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        LEFT JOIN document_content dc ON dc.document_id = d.id
        LEFT JOIN mails       m  ON m.document_id = d.id
        WHERE {like_clauses}
        {filters}
        LIMIT 50
    """
    try:
        rows    = conn.execute(sql, like_params + filter_params).fetchall()
        seen    = set()
        results = []
        for r in rows:
            d = dict(r)
            # Siehe _search_filename(): mehrfach "primaere" Pfade desselben Dokuments
            # koennen den LEFT JOIN sonst vervielfachen.
            if d["id"] in seen:
                continue
            seen.add(d["id"])
            d["fallback"]    = True
            d["page_number"] = None
            d["excerpt"]     = _excerpt(d.pop("raw_content") or "", q)
            results.append(d)
        return results, None
    except Exception as exc:
        return [], _suchfehler(exc, q)


def _excerpt(text: str, query: str, window: int = 220) -> str:
    if not text:
        return ""
    # Gleiche Zerlegung wie die Suche selbst, sonst wird etwas anderes hervorgehoben
    # als gefunden wurde -- bei "u wert" markierte die alte Fassung das erste
    # beliebige "u" im Text, also meist gar nicht die Fundstelle.
    words = [w for w in _TRENNZEICHEN.split(query) if w]
    if not words:
        return text[:window] + ("…" if len(text) > window else "")

    # Zum Hervorheben das ganze getroffene Wort markieren, nicht nur den Wortanfang.
    muster = [
        re.compile(_wortmuster(w).pattern + ("" if len(w) < _MIN_PRAEFIX_LAENGE else r"[^\W_]*"),
                   re.IGNORECASE | re.UNICODE)
        for w in words
    ]
    pos = len(text)
    for m in muster:
        treffer = m.search(text)
        if treffer:
            pos = min(pos, treffer.start())

    start   = max(0, pos - 60)
    end     = min(len(text), start + window)
    snippet = text[start:end]

    for m in muster:
        snippet = m.sub(lambda t: f"<mark>{t.group(0)}</mark>", snippet)

    return ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")


def _search_filename(conn, q: str, filters: str, filter_params: list) -> list[dict]:
    """Sucht per documents_fts nach Dateinamen-Treffern.

    Seit Migration 026 führt documents_fts nur noch die filename-Spalte; der
    Spaltenfilter bleibt trotzdem stehen, er kostet nichts und macht die Absicht
    lesbar. Die Wortzerlegung ist dieselbe wie in _make_fts_query -- inklusive der
    Behandlung von Bindestrichen, an denen FTS5 sonst abbricht."""
    words = [w for w in _TRENNZEICHEN.split(q)
             if w and w.lower() not in _STOPWORDS]
    if not words:
        return []
    fts_q = " AND ".join(f"filename:{_fts_wort(w)}" for w in words)
    sql = f"""
        SELECT
            d.id, d.filename, d.extension, d.filesize, d.modified_at,
            d.extraction_status, d.source_type,
            COALESCE(p.name, m.mailbox_name) AS project_name,
            dp.path     AS filepath,
            NULL        AS raw_content,
            NULL        AS page_number,
            m.sender    AS mail_sender,
            m.date      AS mail_date
        FROM documents_fts
        JOIN  documents       d  ON documents_fts.rowid = d.id
        LEFT JOIN projects        p  ON p.id = d.project_id
        LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        LEFT JOIN mails       m  ON m.document_id = d.id
        WHERE documents_fts MATCH ?
        {filters}
        LIMIT 50
    """
    try:
        rows = conn.execute(sql, [fts_q] + filter_params).fetchall()
        seen    = set()
        results = []
        for r in rows:
            d = dict(r)
            # Verteidigung gegen mehrfach als "primaer" markierte Pfade desselben
            # Dokuments (siehe upsert_path()) -- der LEFT JOIN ... is_primary=1 kann
            # sonst dieselbe Dokument-ID mit unterschiedlichem Pfad mehrfach liefern.
            if d["id"] in seen:
                continue
            seen.add(d["id"])
            d["fallback"] = False
            d["excerpt"]  = ""
            d.pop("raw_content", None)
            results.append(d)
        return results
    except Exception:
        return []


def _search_folders(conn, q: str, project_id: str = "") -> list[dict]:
    """Findet Ordner deren Name die Suchbegriffe enthält (aus document_paths).

    Optimiert: Vorfilterung in SQL (Pfad muss alle Suchwörter enthalten) statt
    alle document_paths in den Speicher zu laden. folder.exists() (NAS-Stat) wird
    nur noch für die wenigen namentlich passenden Treffer aufgerufen, nicht für
    jeden Elternordner jedes Pfades.
    """
    # Dieselbe Zerlegung wie die Dokumentsuche: sonst verhalten sich "u-wert" und
    # "u wert" unterschiedlich -- ersteres suchte den Teilstring "u-wert", letzteres
    # warf das "u" weg und suchte nur "%wert%", was "Bewertung" und
    # "Schalldaemmwerte" mitbrachte.
    words = [w.lower() for w in _TRENNZEICHEN.split(q) if w]
    if not words:
        return []
    # Treffer nur an Wortgrenzen. Kurze Wörter müssen ein ganzes Wort sein -- "u"
    # soll das U in "U-Wert" treffen, nicht das u in "und".
    muster = [_wortmuster(w) for w in words]
    try:
        # Grobfilter in SQL: nur Pfade die ALLE Suchwörter irgendwo enthalten —
        # notwendige Bedingung dafür, dass ein Ordnername alle Wörter enthält.
        sql = """
            SELECT DISTINCT dp.path, p.name AS project_name, p.id AS project_id
            FROM document_paths dp
            JOIN documents d ON d.id = dp.document_id
            JOIN projects p ON p.id = d.project_id
            WHERE 1=1
        """
        params: list = []
        for w in words:
            sql += " AND lower(dp.path) LIKE ?"
            params.append(f"%{w}%")
        if project_id:
            try:
                pid  = int(project_id)
                prow = conn.execute("SELECT path FROM projects WHERE id=?", (pid,)).fetchone()
                if prow and prow["path"] and not str(prow["path"]).startswith("mailbox:"):
                    # Nur Ordner INNERHALB des Projektpfads. Wichtig wegen
                    # Hash-Deduplizierung: ein Dokument kann mehrere Pfade in
                    # verschiedenen Projekten haben (gleiche Datei) — d.project_id
                    # allein würde fremde Projektordner (Duplikate) durchlassen.
                    sql += " AND dp.path LIKE ?"
                    params.append(str(prow["path"]).rstrip("/") + "/%")
                else:
                    sql += " AND d.project_id = ?"
                    params.append(pid)
            except ValueError:
                pass
        sql += " LIMIT 2000"
        rows = conn.execute(sql, params).fetchall()

        seen_dirs: set[str] = set()
        results = []
        for row in rows:
            for folder in Path(row["path"]).parents:
                folder_str = str(folder)
                if folder_str in seen_dirs:
                    continue
                seen_dirs.add(folder_str)
                if all(m.search(folder.name) for m in muster) and folder.exists():
                    results.append({
                        "path":         folder_str,
                        "name":         folder.name,
                        "project_name": row["project_name"],
                        "project_id":   row["project_id"],
                    })
                    if len(results) >= 10:
                        return results
        return results
    except Exception:
        return []


_STOPWORDS = {
    "und", "oder", "der", "die", "das", "dem", "den", "des",
    "ein", "eine", "einen", "einem", "einer", "eines",
    "ist", "sind", "hat", "haben", "wird", "werden",
    "von", "bei", "mit", "zu", "in", "an", "auf", "für",
    "als", "auch", "aber", "noch", "nur", "schon",
    "wenn", "dann", "so", "wie", "was", "wer", "wo",
    "nicht", "kein", "keine", "keinen", "keinem",
    "sich", "es", "er", "sie", "wir", "ihr", "du", "ich",
}

# Alles, was kein Buchstabe und keine Ziffer ist, trennt Wörter. Bewusst als
# Positivliste statt als Liste verbotener Zeichen: der frühere Ansatz zählte einzelne
# Sonderzeichen auf und übersah dabei den Bindestrich -- FTS5 liest "u-wert" als
# Spaltenfilter und die ganze Suche brach mit "no such column: wert" ab. Jedes andere
# Satzzeichen hätte dasselbe Risiko.
#
# Die Trennung entspricht genau dem, was der Indexer macht: der unicode61-Tokenizer
# behandelt alles Nicht-Alphanumerische als Worttrenner. "U-Wert" steht im Index also
# ohnehin als zwei Tokens -- "u wert" findet es, "u-wert" als ein Token niemals.
# Unterstriche gehören dazu ("06_Felix" ist im Index "06" und "felix").
_TRENNZEICHEN = re.compile(r"[\W_]+", re.UNICODE)


def _suchfehler(exc: Exception, q: str) -> str:
    """Übersetzt einen Datenbankfehler in etwas, das man einem Nutzer zeigen kann.

    Vorher landete die rohe SQLite-Meldung in der Oberfläche -- bei einer Suche nach
    "u-wert" stand dort wörtlich »Fehler: no such column: wert«. Das ist für ein
    Architekturbüro nicht nur unverständlich, es lenkt auch vom eigentlichen Problem
    ab. Der genaue Wortlaut geht ins Log, wo er hingehört."""
    logging.getLogger(__name__).warning("Suche fehlgeschlagen (%r): %s", q, exc)
    return ("Die Suche konnte nicht ausgeführt werden. Bitte die Eingabe anders "
            "formulieren — Sonderzeichen können weggelassen werden.")


# Ab wie vielen Zeichen ein Suchwort als Wortanfang (mit *) gesucht wird. Kürzere
# Wörter werden exakt gesucht: "u*" trifft jedes Wort, das mit u beginnt -- und, uns,
# unten -- und macht eine Suche nach "u wert" (also U-Wert) praktisch wertlos. Als
# ganzes Token gesucht trifft "u" nur ein alleinstehendes U, genau wie in "U-Wert",
# das der Indexer ohnehin als "u" + "wert" ablegt.
_MIN_PRAEFIX_LAENGE = 3


def _fts_wort(w: str) -> str:
    return f"{w}*" if len(w) >= _MIN_PRAEFIX_LAENGE else w


def _wortmuster(wort: str, ganzes_wort: bool | None = None) -> "re.Pattern":
    """Sucht ein Wort an einer Wortgrenze -- mit derselben Vorstellung von „Wortgrenze"
    wie der Indexer.

    Warum nicht einfach \\b: für reguläre Ausdrücke ist der Unterstrich ein Wortzeichen,
    für den unicode61-Tokenizer dagegen ein Trenner. In einem Ordner „250813_Attika"
    gäbe es vor „Attika" also kein \\b, obwohl im Index „250813" und „attika" getrennt
    stehen. Deshalb die Grenze selbst definieren: alles ausser Buchstaben und Ziffern
    trennt.

    Kurze Wörter werden als ganzes Wort verlangt (siehe _MIN_PRAEFIX_LAENGE), längere
    dürfen ein Wortanfang sein -- „wert" trifft „Werte", aber nicht „Bewertung"."""
    if ganzes_wort is None:
        ganzes_wort = len(wort) < _MIN_PRAEFIX_LAENGE
    rechts = r"(?![^\W_])" if ganzes_wort else ""
    return re.compile(r"(?<![^\W_])" + re.escape(wort) + rechts,
                      re.IGNORECASE | re.UNICODE)


def _make_fts_query(q: str) -> str:
    words = []
    for w in _TRENNZEICHEN.split(q):
        w = w.strip()
        if w and w.lower() not in _STOPWORDS:
            words.append(w)
    if not words:
        return '""'
    if len(words) == 1:
        return _fts_wort(words[0])

    and_query = " AND ".join(_fts_wort(w) for w in words)

    # Deutsche Komposita: "fenster liste" und "liste fenster" finden beide "Fensterliste".
    # Benachbarte Wortpaare in beiden Reihenfolgen zusammenkleben + vollständige Konkatenation.
    # Nur bis 3 Woerter -- die Heuristik macht bei 4+ Woertern kaum noch treffende Vorschlaege
    # (z.B. "GrauenergieCO2" bei "Grauenergie CO2 Amortisation Solskin"), erzeugt aber pro
    # Wort zwei zusaetzliche OR-Zweige, die FTS5 alle einzeln scannen muss -- bei laengeren
    # Mehrwort-Queries spuerbar langsamer, ohne realistischen Nutzen.
    extras: list[str] = []
    if len(words) <= 3:
        for i in range(len(words) - 1):
            extras.append(f"{words[i]}{words[i + 1]}*")        # vorwärts
            extras.append(f"{words[i + 1]}{words[i]}*")        # rückwärts
        if len(words) > 2:
            extras.append("".join(words) + "*")

    if not extras:
        return and_query
    return "(" + and_query + ") OR " + " OR ".join(extras)
