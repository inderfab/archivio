"""Sicherung und Wiederherstellung einer kompletten Archivio-Installation.

Deckt zwei Faelle ab, die sich denselben Mechanismus teilen:

  * **Sicherung** -- woechentlich automatisch auf den Datei-Server, damit ein
    Rechnerausfall keinen kompletten Neu-Scan von hunderttausenden Dokumenten bedeutet.
  * **Umzug** -- der Server zieht auf einen anderen Mac; die Sicherung wird dort
    eingespielt statt alles neu zu indexieren.

Drei Entscheidungen, die den Aufbau erklaeren:

1. **`VACUUM INTO` statt Dateikopie.** Die DB laeuft im WAL-Modus; ein blosses Kopieren
   von `archivio.db` bei laufendem Server verliert die letzten Schreibvorgaenge
   stillschweigend. `VACUUM INTO` liefert einen konsistenten, kompakten Stand, ohne den
   Server anzuhalten.
2. **Lokal vacuumen, dann uebertragen.** Direkt auf ein Netzlaufwerk zu vacuumen ist um
   ein Vielfaches langsamer (SQLite schreibt Seite fuer Seite ueber SMB) und bricht ab,
   wenn der Mount kurz weg ist. Gemessen: ~229 MB/s lokal.
3. **Genau eine Sicherung, aber nie ohne gueltige Kopie.** Der neue Stand entsteht
   vollstaendig in einem Nebenverzeichnis und wird erst dann per Umbenennen
   eingewechselt. Vorher laeuft `PRAGMA quick_check` -- ohne diese Pruefung wuerde eine
   schleichend defekte Datenbank irgendwann die letzte gute Kopie ueberschreiben.

Mail-Passwoerter werden bewusst NICHT mitgesichert (siehe `_sanitize_config`).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import yaml

from config import settings
from db import connection

log = logging.getLogger(__name__)

BACKUP_DIR_NAME = "Archivio-Sicherung"
_NEW_SUFFIX = ".neu"
_OLD_SUFFIX = ".alt"

DB_FILE = "archivio.db"
CONFIG_FILE = "config.yaml"
MANIFEST_FILE = "manifest.json"

_VERSION_FILE = Path(__file__).parent.parent / "VERSION"


# ── Pfade und Zustand ─────────────────────────────────────────────────────────

def data_dir() -> Path:
    d = os.environ.get("ARCHIVIO_DATA_DIR")
    return Path(d) if d else Path(__file__).parent.parent


def state_path() -> Path:
    return data_dir() / "backup_state.json"


def load_state() -> dict:
    """Laufzeit-Zustand der Sicherung. Bewusst eine eigene Datei und NICHT die
    config.yaml: settings_save() schreibt dort ganze Schluessel neu, Laufzeitwerte
    wuerden bei jedem Speichern der Einstellungen verschwinden."""
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(patch: dict) -> None:
    state = load_state()
    state.update(patch)
    try:
        state_path().parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(state_path())
    except Exception as exc:
        log.warning("backup_state.json nicht schreibbar: %s", exc)


def server_version() -> str:
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return "0.0.0"


def summary() -> dict:
    """Kurzfassung fuer Systemstatus, Diagnose und Einstellungen -- an einer Stelle,
    damit die drei Anzeigen nicht auseinanderlaufen.

    `ok` folgt der Ampel der Diagnose: True gruen, None gelb (noch nichts passiert),
    False rot. Eine stillschweigend kaputte Sicherung ist schlimmer als gar keine,
    deshalb wird sowohl ein Fehlschlag als auch blosses Veralten rot."""
    state = load_state()
    target = (settings.get("backup.path") or "").strip()
    last_run = state.get("last_run")

    if not target:
        return {"configured": False, "ok": None, "text": "Nicht eingerichtet",
                "detail": "In den Einstellungen einen Speicherort festlegen",
                "last_run": None}
    if not last_run:
        return {"configured": True, "ok": None, "text": "Noch keine Sicherung",
                "detail": target, "last_run": None}

    age_days = None
    try:
        age_days = (datetime.now() - datetime.fromisoformat(last_run)).days
    except Exception:
        pass

    size = state.get("bytes") or 0
    when = last_run.replace("T", " ")
    detail = f"{size // 1_000_000} MB nach {state.get('target', target)}"

    if not state.get("last_ok"):
        fails = state.get("consecutive_failures", 1)
        return {"configured": True, "ok": False, "text": f"Fehlgeschlagen ({when})",
                "detail": f"{state.get('error') or 'unbekannter Fehler'} "
                          f"— {fails}× in Folge", "last_run": last_run}
    # Woechentlicher Rhythmus: nach zwei Wochen stimmt etwas nicht.
    if age_days is not None and age_days > 14:
        return {"configured": True, "ok": False, "text": f"Veraltet ({when})",
                "detail": f"Letzte Sicherung vor {age_days} Tagen — {detail}",
                "last_run": last_run}
    return {"configured": True, "ok": True, "text": when, "detail": detail,
            "last_run": last_run}


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def _set(progress: dict | None, **kw) -> None:
    if progress is not None:
        progress.update(kw)


def _quick_check(path: Path) -> str | None:
    """Gibt None zurueck wenn die Datei eine gesunde SQLite-DB ist, sonst den Grund."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
    except Exception as exc:
        return str(exc)
    return None if row and row[0] == "ok" else (row[0] if row else "leeres Ergebnis")


def _sanitize_config(cfg: dict) -> dict:
    """Entfernt alle Mail-Passwoerter aus einer Konfigurationskopie.

    Die Sicherung liegt auf einem Datei-Server, den im Buero viele lesen koennen --
    Zugangsdaten haben dort nichts verloren. Beim Einspielen werden sie neu abgefragt
    (siehe `missing_password_accounts`)."""
    import copy
    cfg = copy.deepcopy(cfg)
    for acc in cfg.get("mail_accounts") or []:
        if isinstance(acc, dict) and acc.get("password"):
            acc["password"] = ""
    if isinstance(cfg.get("mail"), dict) and cfg["mail"].get("password"):
        cfg["mail"]["password"] = ""
    return cfg


def missing_password_accounts(cfg: dict | None = None) -> list[str]:
    """Mailkonten ohne hinterlegtes Passwort -- nach einem Import sind das alle.
    Ohne sichtbaren Hinweis laeuft der Mail-Scan sonst wochenlang ins Leere."""
    cfg = cfg if cfg is not None else settings.load_all()
    out = []
    for acc in cfg.get("mail_accounts") or []:
        if isinstance(acc, dict) and not (acc.get("password") or "").strip():
            out.append(acc.get("label") or acc.get("username") or acc.get("host") or "?")
    return out


def _db_stats(conn: sqlite3.Connection) -> dict:
    def n(sql: str) -> int:
        try:
            return conn.execute(sql).fetchone()[0] or 0
        except Exception:
            return 0
    return {
        "projects":  n("SELECT COUNT(*) FROM projects"),
        "documents": n("SELECT COUNT(*) FROM documents"),
        "chunks":    n("SELECT COUNT(*) FROM document_chunks"),
    }


def _recover_interrupted(parent: Path) -> None:
    """Raeumt einen abgebrochenen Einwechsel-Vorgang auf. Liegt nur noch `.alt` da,
    wurde zwischen den beiden Umbenennungen abgebrochen -- dann ist `.alt` die
    letzte gueltige Sicherung und kommt zurueck."""
    final, old = parent / BACKUP_DIR_NAME, parent / (BACKUP_DIR_NAME + _OLD_SUFFIX)
    if old.exists() and not final.exists():
        log.warning("Abgebrochene Sicherung gefunden -- stelle vorherigen Stand wieder her")
        old.rename(final)


# ── Sicherung erstellen ───────────────────────────────────────────────────────

def create_backup(target_dir: str | Path, progress: dict | None = None) -> dict:
    """Schreibt DB + Einstellungen als Sicherung nach `target_dir`.

    Gibt `{"ok": bool, "error": str|None, ...}` zurueck und haelt denselben Stand in
    `progress` (fuer die Fortschrittsanzeige) und in backup_state.json fest."""
    started = time.time()
    target_dir = Path(target_dir).expanduser()
    _set(progress, running=True, phase="Vorbereiten", error=None, done=False)

    try:
        result = _create_backup_inner(target_dir, progress)
    except Exception as exc:
        log.exception("Sicherung fehlgeschlagen")
        result = {"ok": False, "error": str(exc)}

    result["duration_s"] = round(time.time() - started, 1)
    state = load_state()
    fails = 0 if result["ok"] else int(state.get("consecutive_failures", 0)) + 1
    _save_state({
        "last_run": datetime.now().isoformat(timespec="seconds"),
        "last_ok": result["ok"],
        "error": result.get("error"),
        "bytes": result.get("bytes", 0),
        "duration_s": result["duration_s"],
        "consecutive_failures": fails,
        "target": str(target_dir),
    })
    _set(progress, running=False, done=True, phase="Fertig" if result["ok"] else "Fehler",
         error=result.get("error"), result=result)
    return result


def _create_backup_inner(target_dir: Path, progress: dict | None) -> dict:
    src = connection.db_path()
    if not src.exists():
        return {"ok": False, "error": f"Datenbank nicht gefunden: {src}"}

    if not target_dir.is_dir():
        return {"ok": False, "error": f"Zielordner nicht erreichbar: {target_dir}"}

    _recover_interrupted(target_dir)

    db_size = src.stat().st_size
    tmp_dir = data_dir() / "backup_tmp"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Platz pruefen, bevor irgendetwas angefasst wird. Am Ziel wird das Doppelte
    # gebraucht: die bisherige Sicherung bleibt liegen, bis die neue vollstaendig ist.
    if shutil.disk_usage(tmp_dir).free < db_size * 1.1:
        return {"ok": False, "error":
                f"Zu wenig lokaler Speicher: {db_size // 1_000_000} MB werden gebraucht"}
    if shutil.disk_usage(target_dir).free + _existing_backup_size(target_dir) < db_size * 2:
        return {"ok": False, "error":
                f"Zu wenig Platz am Zielort: etwa {db_size * 2 // 1_000_000} MB noetig "
                "(die bisherige Sicherung bleibt erhalten, bis die neue vollstaendig ist)"}

    try:
        # 1) Konsistenter, kompakter Stand -- lokal, bei laufendem Server
        _set(progress, phase="Datenbank kopieren")
        tmp_db = tmp_dir / DB_FILE
        conn = connection.get_connection()
        try:
            conn.execute("VACUUM INTO ?", (str(tmp_db),))
        finally:
            conn.close()

        # 2) Erst pruefen, dann einwechseln
        _set(progress, phase="Kopie pruefen")
        problem = _quick_check(tmp_db)
        if problem:
            return {"ok": False, "error":
                    f"Die erstellte Kopie ist fehlerhaft ({problem}) -- die bisherige "
                    "Sicherung wurde nicht angetastet."}

        # 3) Einstellungen ohne Passwoerter + Manifest
        cfg = _sanitize_config(settings.load_all())
        (tmp_dir / CONFIG_FILE).write_text(
            yaml.dump(cfg, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8")

        check = sqlite3.connect(tmp_db)
        try:
            stats = _db_stats(check)
            migrations = [r[0] for r in check.execute("SELECT id FROM _migrations ORDER BY id")]
        finally:
            check.close()
        manifest = {
            "server_version": server_version(),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "hostname": os.uname().nodename,
            "stats": stats,
            "base_folders": [f.get("path", "") for f in (settings.get("scanner.base_folders") or [])],
            "migrations": migrations,
            "db_bytes": tmp_db.stat().st_size,
        }
        (tmp_dir / MANIFEST_FILE).write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

        # 4) Uebertragen -- vollstaendig daneben, dann in zwei Umbenennungen einwechseln
        _set(progress, phase="Auf Zielordner uebertragen")
        final = target_dir / BACKUP_DIR_NAME
        staging = target_dir / (BACKUP_DIR_NAME + _NEW_SUFFIX)
        old = target_dir / (BACKUP_DIR_NAME + _OLD_SUFFIX)
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)
        staging.mkdir(parents=True)
        for name in (DB_FILE, CONFIG_FILE, MANIFEST_FILE):
            shutil.copy2(tmp_dir / name, staging / name)

        _set(progress, phase="Abschliessen")
        if final.exists():
            final.rename(old)
        staging.rename(final)
        shutil.rmtree(old, ignore_errors=True)

        log.info("Sicherung geschrieben: %s (%d Dokumente, %d MB)",
                 final, stats["documents"], manifest["db_bytes"] // 1_000_000)
        return {"ok": True, "error": None, "path": str(final),
                "bytes": manifest["db_bytes"], "stats": stats}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _existing_backup_size(target_dir: Path) -> int:
    """Platz, der beim Einwechseln wieder frei wird (die abgeloeste Sicherung)."""
    db = target_dir / BACKUP_DIR_NAME / DB_FILE
    try:
        return db.stat().st_size
    except Exception:
        return 0


# ── Sicherung lesen / pruefen ─────────────────────────────────────────────────

def read_manifest(source_dir: str | Path) -> dict | None:
    p = Path(source_dir).expanduser()
    if p.name != BACKUP_DIR_NAME and (p / BACKUP_DIR_NAME).is_dir():
        p = p / BACKUP_DIR_NAME
    try:
        return json.loads((p / MANIFEST_FILE).read_text(encoding="utf-8"))
    except Exception:
        return None


def resolve_backup_dir(source_dir: str | Path) -> Path:
    """Erlaubt sowohl den Sicherungsordner selbst als auch dessen Elternordner --
    der Ordnerdialog liefert je nachdem das eine oder das andere."""
    p = Path(source_dir).expanduser()
    if p.name != BACKUP_DIR_NAME and (p / BACKUP_DIR_NAME).is_dir():
        return p / BACKUP_DIR_NAME
    return p


# ── Pfade umschreiben ─────────────────────────────────────────────────────────

# Jede Stelle, an der ein absoluter Pfad in der Datenbank steht. block_rules ist
# bewusst eingeschraenkt: nur type='folder' haelt einen Pfad, bei 'file' steht ein
# SHA256, bei 'pattern' ein Glob und bei 'project' eine Id.
_PATH_COLUMNS: list[tuple[str, str, str]] = [
    ("projects",       "path",  ""),
    ("document_paths", "path",  ""),
    ("ignored_paths",  "path",  ""),
    ("norm_folders",   "path",  ""),
    ("block_rules",    "value", "type = 'folder'"),
]


def rewrite_paths(conn: sqlite3.Connection, mapping: dict[str, str]) -> dict[str, int]:
    """Ersetzt alte Pfad-Praefixe durch neue -- noetig, wenn der Datei-Server auf dem
    Zielrechner anders eingehaengt ist. Ohne das zeigt der komplette Index ins Leere:
    nichts laesst sich oeffnen, keine Vorschau, und ein Neu-Scan wuerde die verwaisten
    Eintraege samt Foto-Tags und Bewertungen loeschen."""
    counts: dict[str, int] = {}
    for table, col, extra in _PATH_COLUMNS:
        try:
            conn.execute(f"SELECT {col} FROM {table} LIMIT 1")
        except sqlite3.Error:
            continue  # Tabelle existiert in dieser Schema-Version noch nicht
        total = 0
        for old, new in mapping.items():
            old_s, new_s = old.rstrip("/"), new.rstrip("/")
            if not old_s or old_s == new_s:
                continue
            where = f"({col} = ? OR {col} LIKE ?)" + (f" AND {extra}" if extra else "")
            cur = conn.execute(
                f"UPDATE {table} SET {col} = ? || substr({col}, ?) WHERE {where}",
                (new_s, len(old_s) + 1, old_s, old_s + "/%"),
            )
            total += cur.rowcount
        if total:
            counts[f"{table}.{col}"] = total
    return counts


def rewrite_config_paths(cfg: dict, mapping: dict[str, str]) -> dict:
    """Dasselbe fuer die Einstellungen. `scanner.excluded_folders` bleibt unangetastet --
    das sind reine Ordnernamen bzw. Globs und damit ohnehin portabel."""
    def swap(value: str) -> str:
        for old, new in mapping.items():
            old_s, new_s = old.rstrip("/"), new.rstrip("/")
            if old_s and (value == old_s or value.startswith(old_s + "/")):
                return new_s + value[len(old_s):]
        return value

    for folder in cfg.get("scanner", {}).get("base_folders") or []:
        if isinstance(folder, dict) and folder.get("path"):
            folder["path"] = swap(folder["path"])
    for proj in cfg.get("projects") or []:
        if isinstance(proj, dict) and proj.get("path"):
            proj["path"] = swap(proj["path"])
    rub = cfg.get("rubrica")
    if isinstance(rub, dict) and rub.get("db_path"):
        rub["db_path"] = swap(rub["db_path"])
    return cfg


# ── Sicherung einspielen ──────────────────────────────────────────────────────

def import_backup(source_dir: str | Path, *, confirm_replace: bool = False,
                  path_map: dict[str, str] | None = None,
                  progress: dict | None = None) -> dict:
    """Spielt eine Sicherung ein -- ersetzt Datenbank UND Einstellungen.

    Bewusst kein Zusammenfuehren: alles haengt an fortlaufenden Ids (Projekte,
    Dokumente, Chunks, Foto-Tags, Sperrregeln), und in beiden echten Anwendungsfaellen
    -- neuer Mac oder Wiederherstellung nach Ausfall -- ist die Zielseite leer oder
    soll verworfen werden.

    Alle Pruefungen laufen vollstaendig durch, BEVOR irgendetwas veraendert wird.
    Gibt bei fehlender Bestaetigung bzw. fehlenden Pfaden eine Rueckfrage zurueck
    statt abzubrechen."""
    _set(progress, running=True, phase="Pruefen", error=None, done=False)
    try:
        result = _import_backup_inner(source_dir, confirm_replace, path_map or {}, progress)
    except Exception as exc:
        log.exception("Import fehlgeschlagen")
        result = {"ok": False, "error": str(exc)}
    if result.get("ok"):
        phase = "Fertig"
    elif result.get("needs_confirm") or result.get("needs_path_mapping"):
        phase = "Rückfrage"      # keine Störung, sondern eine offene Frage an die Oberfläche
    else:
        phase = "Abgebrochen"
    _set(progress, running=False, done=True, error=result.get("error"),
         phase=phase, result=result)
    return result


def _paths_to_verify(manifest: dict, src_db: Path) -> list[str]:
    """Welche Ordner muessen auf dem Zielrechner existieren, damit der Index stimmt?

    Erste Wahl sind die Basisordner aus dem Manifest. Sind dort keine eingetragen --
    das kommt vor, etwa wenn Projekte einzeln angelegt wurden --, dienen ersatzweise
    die Elternordner der Projekte. Ohne diesen Rueckfall wuerde bei leerer
    Basisordner-Liste ueberhaupt nicht geprueft, und ein Umzug landete stillschweigend
    bei einem Index, dessen Pfade alle ins Leere zeigen."""
    base = [p for p in (manifest.get("base_folders") or []) if p]
    if base:
        return sorted(set(base))
    try:
        conn = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT DISTINCT path FROM projects").fetchall()
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Projektpfade der Sicherung nicht lesbar: %s", exc)
        return []
    parents = {str(Path(r[0]).parent) for r in rows if r[0]}
    return sorted(p for p in parents if p not in ("/", "."))


def _import_backup_inner(source_dir, confirm_replace: bool,
                         path_map: dict[str, str], progress: dict | None) -> dict:
    from packaging.version import Version

    src_dir = resolve_backup_dir(source_dir)
    src_db = src_dir / DB_FILE
    manifest = read_manifest(src_dir)
    if manifest is None or not src_db.exists():
        return {"ok": False, "error":
                f"Keine gueltige Sicherung in {src_dir} (manifest.json oder {DB_FILE} fehlt)"}

    # 1) Versionsvergleich -- Migrationen laufen nur vorwaerts
    backup_version, own = manifest.get("server_version", "0.0.0"), server_version()
    try:
        if Version(backup_version) > Version(own):
            return {"ok": False, "error":
                    f"Die Sicherung stammt aus einer neueren Archivio-Version "
                    f"({backup_version} > {own}). Bitte diesen Server zuerst aktualisieren."}
    except Exception:
        log.warning("Versionsvergleich nicht moeglich: %r vs %r", backup_version, own)

    # 2) Zielzustand -- nur bei bereits vorhandenen Daten nachfragen
    dest_db = connection.db_path()
    target_stats = {"projects": 0, "documents": 0, "chunks": 0}
    if dest_db.exists():
        conn = sqlite3.connect(dest_db)
        try:
            target_stats = _db_stats(conn)
        finally:
            conn.close()
    if target_stats["projects"] > 0 and not confirm_replace:
        return {"ok": False, "needs_confirm": True,
                "target_stats": target_stats, "backup_stats": manifest.get("stats", {}),
                "backup_created_at": manifest.get("created_at"),
                "error": None}

    # 3) Pfadpruefung -- fehlende Ordner muessen zugeordnet werden
    to_check = _paths_to_verify(manifest, src_db)
    missing = [p for p in to_check if p not in path_map and not Path(p).exists()]
    if missing:
        return {"ok": False, "needs_path_mapping": missing,
                "target_stats": target_stats, "backup_stats": manifest.get("stats", {}),
                "error": None}

    # 4) Ist die mitgebrachte Datenbank gesund?
    #
    # Bewusst ERST HIER, nach allen Rueckfragen: die Pruefung liest die komplette
    # Datei und dauert bei mehreren GB von einer externen Platte Minuten (real
    # gemessen: 253 s fuer 5,6 GB). Lief sie vor den Rueckfragen, wartete man diese
    # Zeit ab, bevor ueberhaupt nach dem fehlenden Ordner gefragt wurde -- und nach
    # der Antwort gleich nochmal. Vor jeder Veraenderung laeuft sie weiterhin.
    _set(progress, phase="Sicherung wird geprüft")
    problem = _quick_check(src_db)
    if problem:
        return {"ok": False, "error": f"Die Sicherung ist beschaedigt: {problem}"}

    # ── ab hier wird veraendert ──
    _set(progress, phase="Bisherigen Stand sichern")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safety = None
    if dest_db.exists():
        # Umbenennen statt ueberschreiben: der bisherige Stand koennte die letzte
        # Kopie sein. Wird bewusst NICHT automatisch geloescht.
        #
        # Die WAL-Datei muss dabei mit -- sie enthaelt die zuletzt geschriebenen
        # Transaktionen. Wird sie zurueckgelassen oder geloescht, ist die
        # Sicherheitskopie beim Oeffnen unbrauchbar ("disk I/O error"). Erst
        # versuchen, sie in die Hauptdatei zu falten; klappt das wegen anderer
        # offener Verbindungen nicht, wandert sie unter dem neuen Namen mit.
        try:
            conn = sqlite3.connect(dest_db)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
        except Exception as exc:
            log.warning("WAL-Checkpoint vor dem Import fehlgeschlagen: %s", exc)

        safety = dest_db.with_name(f"{dest_db.name}.vor-import-{stamp}")
        dest_db.rename(safety)
        for suffix in ("-wal", "-shm"):
            sidecar = dest_db.with_name(dest_db.name + suffix)
            if sidecar.exists():
                sidecar.rename(safety.with_name(safety.name + suffix))

    _set(progress, phase="Datenbank einspielen")
    old_cfg = settings.load_all()
    shutil.copy2(src_db, dest_db)

    # Schema nachziehen, falls die Sicherung aus einer aelteren Version stammt
    _set(progress, phase="Schema aktualisieren")
    connection.init_schema()

    _set(progress, phase="Pfade anpassen")
    rewritten: dict[str, int] = {}
    if path_map:
        conn = connection.get_connection()
        try:
            with conn:
                rewritten = rewrite_paths(conn, path_map)
        finally:
            conn.close()

    # Einstellungen uebernehmen -- mit zwei bewussten Ausnahmen, die zum Rechner
    # gehoeren und nicht zur Sicherung: wo die Datenbank liegt und auf welcher
    # Adresse der Server horcht.
    _set(progress, phase="Einstellungen uebernehmen")
    new_cfg = {}
    try:
        new_cfg = yaml.safe_load((src_dir / CONFIG_FILE).read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.warning("config.yaml der Sicherung nicht lesbar (%s) -- Einstellungen bleiben", exc)
    if new_cfg:
        if path_map:
            new_cfg = rewrite_config_paths(new_cfg, path_map)
        new_cfg["database"] = old_cfg.get("database") or {"path": "archivio.db"}
        if old_cfg.get("server"):
            new_cfg["server"] = old_cfg["server"]
        rub = new_cfg.get("rubrica")
        if isinstance(rub, dict) and rub.get("db_path") and not Path(rub["db_path"]).parent.exists():
            rub["db_path"] = ""   # faellt auf "neben archivio.db" zurueck
        settings.config_path().write_text(
            yaml.dump(new_cfg, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8")
        settings.reload()

    conn = connection.get_connection()
    try:
        stats = _db_stats(conn)
    finally:
        conn.close()

    _save_state({"last_import": datetime.now().isoformat(timespec="seconds"),
                 "last_import_from": str(src_dir)})
    log.info("Sicherung eingespielt: %s (%d Projekte, %d Dokumente)",
             src_dir, stats["projects"], stats["documents"])
    return {
        "ok": True, "error": None,
        "stats": stats,
        "rewritten": rewritten,
        "safety_copy": str(safety) if safety else None,
        "missing_passwords": missing_password_accounts(new_cfg or None),
        "backup_created_at": manifest.get("created_at"),
    }
