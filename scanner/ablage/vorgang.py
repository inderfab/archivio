"""Vorgänge des Datei-Drops auf dem Server: Analyse, Bestätigung, Abschluss, Aufräumen.

Ein Vorgang = eine abgelegte Datei von der Analyse bis zur Ablage (24 h). Der Server rechnet den Vorschlag,
speichert ihn und prüft den Entscheid des Nutzers (`bestaetigen`): das Ziel muss in einem Projekt liegen.
Ausgeführt wird die Ablage vom Helper (oder, bei einem Drop am Server-Mac, vom Server selbst) über
`shared/ablage_transport.py`.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db import connection
from scanner.ablage import kontext, vorlage
from scanner.ablage import merkmale as mk
from scanner.ablage.normalisieren import pfad_schluessel
from scanner.ablage.projekt import DateiInfo
from scanner.ablage.vorschlag import ordner_vorschlag, vorgaenger_im_ordner, vorschlagen

log = logging.getLogger(__name__)

EXTRAKTION_TIMEOUT_S = 3        # hart: danach geht es ohne Inhalt weiter (Auftrag §7)
TTL_STUNDEN = 24
_TOKEN_RE = None


def _transport():
    """`shared/ablage_transport.py` liegt im Bundle flach neben den Paketen, im Repo unter shared/."""
    try:
        import ablage_transport
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "shared"))
        import ablage_transport
    return ablage_transport


def _jetzt() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def staging_verzeichnis() -> Path:
    return connection.db_path().parent / "ablage_staging"


def token_gueltig(token) -> bool:
    return isinstance(token, str) and 8 <= len(token) <= 64 and all(c.isalnum() or c in "-_" for c in token)


# ── Extraktion mit hartem Limit ────────────────────────────────────────────────

def text_extrahieren(pfad: Path, timeout: float = EXTRAKTION_TIMEOUT_S) -> str | None:
    """Die ersten ~3000 Zeichen Text, höchstens `timeout` Sekunden lang. Nie ein Fehler nach aussen."""
    from scanner import extractors
    box: list = []

    def _arbeit():
        try:
            chunks = extractors.extract_chunks(pfad)
            box.append("\n".join(c["content"] for c in chunks)[:mk.INHALT_ZEICHEN])
        except Exception as exc:
            log.debug("Extraktion beim Drop nicht möglich (%s): %s", pfad.name, exc)

    t = threading.Thread(target=_arbeit, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        log.info("Extraktion beim Drop nach %.0f s abgebrochen: %s", timeout, pfad.name)
        return None
    return box[0] if box and box[0] else None


# ── Projektzuordnung eines Zielpfads ───────────────────────────────────────────

def projekt_fuer_pfad(conn, pfad: str):
    """Projekt, dessen Pfad `pfad` enthält (längster Treffer). Postfächer und der Musterordner zählen nie."""
    schluessel = pfad_schluessel(pfad)
    bestes = None
    for r in conn.execute("SELECT id, name, path FROM projects"):
        p = pfad_schluessel(r["path"])
        if r["path"].startswith("mailbox:") or vorlage.ist_musterprojekt(r["path"]):
            continue
        if schluessel == p or schluessel.startswith(p.rstrip("/") + "/"):
            if bestes is None or len(p) > len(pfad_schluessel(bestes["path"])):
                bestes = r
    return bestes


# ── .eml: Absender, Betreff, Datum ─────────────────────────────────────────────

_MAX_EML_BYTES = 524288      # wie der Extraktor: Header und Text, nie die Anhänge


def mail_aus_eml(pfad: Path) -> dict | None:
    """Absender, Betreff und Datum einer .eml mit der vorhandenen Mail-Logik (`scanner/mail_scanner.py`, kein neuer
    Parser). Gibt die Felder zurück, die `merkmale()` für `dom:` und `tok:` braucht; bei Fehlern None."""
    import email
    from scanner import mail_scanner as ms
    try:
        with open(pfad, "rb") as f:
            msg = email.message_from_bytes(f.read(_MAX_EML_BYTES))
        name, adresse = ms.split_addresses(msg.get("From", ""))
        sender = f"{name} <{adresse}>".strip(" <>") if adresse else name
        mail = {"sender": sender, "subject": ms.decode_mime_header(msg.get("Subject", "")),
                "datum": ms.parse_mail_date(msg.get("Date", ""))}
    except Exception as exc:
        log.debug("eml nicht lesbar (%s): %s", pfad.name, exc)
        return None
    return mail if (mail["sender"] or mail["subject"]) else None


# ── Analyse ────────────────────────────────────────────────────────────────────

def analysieren(conn, name: str, groesse: int | None, mtime: str | None, hash_: str | None, host: str | None,
                datei_pfad: Path | None = None, lokal_src: str | None = None, par=None) -> dict:
    """Vorschlag rechnen und als Vorgang speichern. `datei_pfad`: hochgeladene Kopie im Staging (oder die lokale
    Quelle bei einem Drop am Server-Mac). Gibt {token, vorschlag, projekt_pfad?} zurück."""
    aufraeumen(conn)
    ctx = kontext.holen_wartend(conn)                  # höchstens ~2 s warten, sonst „Vorschläge werden vorbereitet"
    text = text_extrahieren(datei_pfad) if datei_pfad is not None else None
    mail = mail_aus_eml(datei_pfad) if datei_pfad is not None and name.lower().endswith(".eml") else None
    datei = DateiInfo(name, groesse, mtime, hash_, text, mail, host)
    if ctx is None:
        datei.merkmale = mk.merkmale(name, groesse, mtime, text, mail)
        vs = {"fall": "vorbereitung", "projekte": [], "sicher_bis": None, "optionen": [], "dateiname_vorschlag": None}
    else:
        datei.merkmale = mk.merkmale(name, groesse, mtime, text, mail,
                                     bekannte_projekte={str(n) for n in ctx._nummern})
        vs = vorschlagen(ctx, datei, par)
    if vs.get("projekte") and vs["projekte"][0]["p"] >= (par.projekt_sicher if par else 0.6) \
            and vs["fall"] != "duplikat":
        try:
            from web.dashboard import _render_filename_template
            vorschlag_name = _render_filename_template(
                connection_setting("upload.filename_template") or "{dateiname}",
                project_name=vs["projekte"][0]["name"], original_name=name)
            vs["dateiname_vorschlag"] = None if vorschlag_name == name else vorschlag_name
        except Exception as exc:
            log.debug("Namensvorschlag nicht möglich: %s", exc)
    token = secrets.token_urlsafe(18)
    with conn:
        conn.execute(
            "INSERT INTO ablage_vorgang (token, erstellt, host, dateiname, groesse, mtime, hash, staging_pfad,"
            " lokal_src, merkmale, vorschlag) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (token, _jetzt(), host, name, groesse, mtime, hash_, str(datei_pfad) if datei_pfad else None,
             lokal_src, json.dumps(datei.merkmale), json.dumps(vs)))
    return {"token": token, "vorschlag": vs}


def connection_setting(key: str):
    from config import settings
    return settings.get(key)


def analyse_upload(conn, name: str, groesse, mtime, hash_, host, daten: bytes | None, par=None) -> dict:
    """Upload der Helper-Seite: Datei ins Staging schreiben, dann analysieren."""
    pfad = None
    if daten is not None:
        verz = staging_verzeichnis() / secrets.token_urlsafe(8)
        verz.mkdir(parents=True, exist_ok=True)
        pfad = verz / (Path(name).name or "datei")
        pfad.write_bytes(daten)
    return analysieren(conn, Path(name).name, groesse, mtime, hash_, host, pfad, None, par)


def analyse_lokal(conn, src: str, host: str | None = "server", par=None) -> dict:
    """Drop direkt am Server-Mac: der Server liest die Datei selbst. Dieselbe Engine, nur ohne Upload."""
    p = Path(src)
    if not p.is_file():
        raise FileNotFoundError(f"Datei nicht gefunden: {src}")
    st = p.stat()
    th = _transport()
    return analysieren(conn, p.name, st.st_size,
                       datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       th.sha256_datei(str(p)), host, p, str(p), par)


# ── Lesen ──────────────────────────────────────────────────────────────────────

def holen(conn, token: str) -> dict | None:
    if not token_gueltig(token):
        return None
    r = conn.execute("SELECT * FROM ablage_vorgang WHERE token = ?", (token,)).fetchone()
    if not r:
        return None
    d = dict(r)
    for k in ("merkmale", "vorschlag", "entscheid"):
        d[k] = json.loads(d[k]) if d.get(k) else None
    return d


# ── Bestätigen ─────────────────────────────────────────────────────────────────

MODI = ("behalten", "archivieren", "ueberschreiben")


def gleichnamige(ctx, projekt_id: int, dest: str, dateiname: str) -> dict | None:
    """Liegt im Zielordner schon eine Datei mit diesem Namen (ausser Datum)? Zuerst der Index, dann ein einziges
    `is_file()` auf den exakten Namen (die Datei kann neu und noch nicht gescannt sein).
    Rückgabe: {vorgaenger, dateiname, archiv_ordner, exakt} oder None. `exakt` = identischer Dateiname (nur dann
    ist „überschreiben" möglich)."""
    from scanner.ablage.vorschlag import _archiv_kind
    treffer = vorgaenger_im_ordner(ctx, projekt_id, dateiname, dest)
    ordner_id = next((o.id for o in ctx.ordner.values() if o.project_id == projekt_id and o.path == dest), None) \
        if treffer is None else None
    if treffer is None:
        p = Path(dest) / dateiname
        if p.is_file():
            return {"vorgaenger": str(p), "dateiname": dateiname, "exakt": True,
                    "archiv_ordner": _archiv_kind(ctx, ordner_id)}
        return None
    treffer["exakt"] = treffer["dateiname"] == dateiname
    return treffer


def bestaetigen(conn, token: str, entscheid: dict) -> tuple[bool, str]:
    """Prüft und speichert den Entscheid des Nutzers. Das Ziel muss in einem Projekt liegen.

    Erwartet: dest (Ordner), dateiname, kopie (bool), vorgaenger_modus (behalten|archivieren|ueberschreiben),
    optional vorgaenger/archiv_ordner (werden nochmals gegen den Bestand geprüft, nicht blind übernommen).
    """
    th = _transport()
    v = holen(conn, token)
    if v is None:
        return False, "Unbekannter Vorgang"
    if v["status"] not in ("offen", "bestaetigt"):
        return False, "Vorgang ist bereits abgeschlossen"
    dest = pfad_schluessel((entscheid.get("dest") or "").strip())
    dateiname = (entscheid.get("dateiname") or v["dateiname"]).strip()
    if not os.path.isabs(dest) or ".." in Path(dest).parts:
        return False, "Ungültiger Zielpfad"
    if not th.dateiname_ok(dateiname):
        return False, "Ungültiger Dateiname"
    projekt = projekt_fuer_pfad(conn, dest)
    if projekt is None:
        return False, ("Dieser Ordner gehört zu keinem Archivio-Projekt — bitte einen Ordner innerhalb "
                       "eines bestehenden Projekts wählen.")
    if not (os.path.isdir(dest) or os.path.isdir(os.path.dirname(dest))):
        return False, "Zielordner nicht gefunden"
    modus = entscheid.get("vorgaenger_modus") or "behalten"
    if modus not in MODI:
        return False, "Ungültige Wahl bei gleichnamiger Datei"
    vorgaenger = archiv = None
    if modus != "behalten":
        ctx = kontext.holen(conn)
        treffer = gleichnamige(ctx, projekt["id"], dest, dateiname)
        if treffer is None or (modus == "ueberschreiben" and not treffer["exakt"]):
            modus = "behalten"            # nichts da, was archiviert/überschrieben werden könnte
        else:
            vorgaenger, archiv = treffer["vorgaenger"], treffer["archiv_ordner"]
    gespeichert = {"dest": dest, "dateiname": dateiname, "kopie": bool(entscheid.get("kopie")),
                   "vorgaenger_modus": modus, "vorgaenger": vorgaenger, "archiv_ordner": archiv,
                   "projekt_id": projekt["id"], "projekt_pfad": projekt["path"]}
    with conn:
        conn.execute("UPDATE ablage_vorgang SET entscheid = ?, status = 'bestaetigt' WHERE token = ?",
                     (json.dumps(gespeichert), token))
    return True, ""


def entscheid_fuer_helper(conn, token: str) -> dict | None:
    """Was der Helper ausführen darf: nur ein bestätigter Entscheid, mit Projektpfad zur Gegenprobe."""
    v = holen(conn, token)
    if v is None:
        return None
    e = v["entscheid"] or {}
    return {"status": v["status"], **{k: e.get(k) for k in (
        "dest", "dateiname", "kopie", "vorgaenger_modus", "vorgaenger", "archiv_ordner", "projekt_pfad")}}


# ── Abschluss ──────────────────────────────────────────────────────────────────

def _rang(vorschlag: dict, dest: str) -> int:
    for i, o in enumerate(vorschlag.get("optionen") or [], 1):
        p = pfad_schluessel(o.get("pfad") or "")
        if p and (dest == p or dest.startswith(p.rstrip("/") + "/") or p.startswith(dest.rstrip("/") + "/")):
            return i
    return 0


def abgelegt(conn, token: str, final_path: str | None, trocken: bool = False,
             archiviert: str | None = None) -> tuple[bool, str]:
    """Der Helper (oder der Server) meldet: abgelegt. Protokollieren, Datei sofort indexieren, aufräumen.
    Ein Trockenlauf protokolliert nur und lässt den Vorgang offen."""
    v = holen(conn, token)
    if v is None:
        return False, "Unbekannter Vorgang"
    e = v["entscheid"]
    if v["status"] != "bestaetigt" or not e:
        return False, "Vorgang ist nicht bestätigt"
    if final_path and not projekt_fuer_pfad(conn, final_path):
        return False, "Ablageort liegt in keinem Projekt"
    if trocken:
        log.info("TROCKENLAUF Ablage: %s -> %s", v["dateiname"], final_path)
        return True, ""
    dauer = int((datetime.now(timezone.utc) - datetime.strptime(v["erstellt"], "%Y-%m-%dT%H:%M:%SZ")
                 .replace(tzinfo=timezone.utc)).total_seconds() * 1000)
    vs = v["vorschlag"] or {}
    erster = (vs.get("projekte") or [{}])[0]
    with conn:
        cur = conn.execute(
            "INSERT INTO ablage_log (ts, host, dateiname, endung, merkmale, vorschlag, gewaehlt_pfad, gewaehlt_rang,"
            " projekt_richtig, dauer_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_jetzt(), v["host"], v["dateiname"], Path(v["dateiname"]).suffix.lower(), json.dumps(v["merkmale"]),
             json.dumps(vs), e["dest"], _rang(vs, e["dest"]), int(erster.get("id") == e.get("projekt_id")), dauer))
        e.update(final_path=final_path, abgelegt_ts=_jetzt(), log_id=cur.lastrowid, archiviert=archiviert)
        conn.execute("UPDATE ablage_vorgang SET status = 'abgelegt', entscheid = ? WHERE token = ?",
                     (json.dumps(e), token))
    if final_path and os.path.isfile(final_path):
        try:
            from scanner.walker import process_file
            process_file(conn, e["projekt_id"], Path(final_path))      # sofort durchsuchbar
        except Exception as exc:
            log.warning("Sofort-Indexierung fehlgeschlagen für %s: %s", final_path, exc)
    _staging_loeschen(v.get("staging_pfad"))
    return True, ""


def _staging_loeschen(pfad: str | None) -> None:
    if not pfad:
        return
    p = Path(pfad)
    try:
        if staging_verzeichnis() in p.parents:
            shutil.rmtree(p.parent, ignore_errors=True)
    except Exception:
        pass


def abbrechen(conn, token: str) -> None:
    v = holen(conn, token)
    if v and v["status"] in ("offen", "bestaetigt"):
        with conn:
            conn.execute("UPDATE ablage_vorgang SET status = 'abgebrochen' WHERE token = ?", (token,))
        _staging_loeschen(v.get("staging_pfad"))


def aufraeumen(conn, stunden: int = TTL_STUNDEN) -> int:
    """Vorgänge und Staging-Dateien älter als `stunden` entfernen (das Protokoll `ablage_log` bleibt)."""
    grenze = (datetime.now(timezone.utc) - timedelta(hours=stunden)).strftime("%Y-%m-%dT%H:%M:%SZ")
    alt = conn.execute("SELECT token, staging_pfad FROM ablage_vorgang WHERE erstellt < ?", (grenze,)).fetchall()
    for r in alt:
        _staging_loeschen(r["staging_pfad"])
    if alt:
        with conn:
            conn.execute("DELETE FROM ablage_vorgang WHERE erstellt < ?", (grenze,))
    # Verwaiste Staging-Ordner (Vorgang weg, Verzeichnis geblieben)
    verz = staging_verzeichnis()
    if verz.is_dir():
        offen = {Path(r["staging_pfad"]).parent.name for r in conn.execute(
            "SELECT staging_pfad FROM ablage_vorgang WHERE staging_pfad IS NOT NULL")}
        for d in verz.iterdir():
            try:
                if d.is_dir() and d.name not in offen and \
                        datetime.fromtimestamp(d.stat().st_mtime, timezone.utc) < datetime.now(timezone.utc) - timedelta(hours=stunden):
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass
    return len(alt)


# ── Rückgängig (Buchhaltung auf dem Server) ────────────────────────────────────

def rueckgaengig_buchen(conn, token: str, final_path: str | None = None) -> bool:
    """Nach einem Rückgängig (Helper oder Server): Vorgang auf 'abgebrochen', Protokollzeile weg (der Nutzer hat
    den Entscheid ja zurückgenommen), die Datei am Ablageort aus dem Index entfernen."""
    v = holen(conn, token)
    if v is None or v["status"] != "abgelegt":
        return False
    e = v["entscheid"] or {}
    pfad = final_path or e.get("final_path")
    with conn:
        if e.get("log_id"):
            conn.execute("DELETE FROM ablage_log WHERE id = ?", (e["log_id"],))
        conn.execute("UPDATE ablage_vorgang SET status = 'abgebrochen' WHERE token = ?", (token,))
    if pfad:
        _aus_index_entfernen(conn, pfad)
    return True


def _aus_index_entfernen(conn, pfad: str) -> None:
    """Pfad aus dem Index nehmen, damit es keine Suchtreffer auf eine verschobene Datei gibt (der nächste Scan
    würde es ohnehin bereinigen)."""
    r = conn.execute("SELECT id, document_id FROM document_paths WHERE path = ?", (pfad,)).fetchone()
    if not r:
        return
    with conn:
        conn.execute("DELETE FROM document_paths WHERE id = ?", (r["id"],))
        if not conn.execute("SELECT 1 FROM document_paths WHERE document_id = ?", (r["document_id"],)).fetchone():
            conn.execute("DELETE FROM documents WHERE id = ?", (r["document_id"],))


def rueckgaengig_lokal(conn, token: str) -> tuple[int, dict]:
    """Drop am Server-Mac: der Server macht die Ablage selbst rückgängig (5 Minuten, nicht bei Kopie/Überschreiben)."""
    v = holen(conn, token)
    if v is None or not v.get("lokal_src"):
        return 404, {"ok": False, "error": "Kein lokaler Vorgang"}
    e = v["entscheid"] or {}
    if v["status"] != "abgelegt" or not e.get("final_path"):
        return 409, {"ok": False, "error": "Nichts rückgängig zu machen"}
    alter = (datetime.now(timezone.utc) - datetime.strptime(e["abgelegt_ts"], "%Y-%m-%dT%H:%M:%SZ")
             .replace(tzinfo=timezone.utc)).total_seconds()
    th = _transport()
    if alter > th.RUECKGAENGIG_S:
        return 409, {"ok": False, "error": "Die 5 Minuten sind vorbei"}
    if e.get("kopie"):
        return 409, {"ok": False, "error": "Bei einer Kopie gibt es nichts rückgängig zu machen"}
    if e.get("vorgaenger_modus") == "ueberschreiben" and e.get("vorgaenger"):
        return 409, {"ok": False, "error": "Überschriebene Datei lässt sich nicht zurückholen"}
    final, src = Path(e["final_path"]), Path(v["lokal_src"])
    if not final.is_file():
        return 404, {"ok": False, "error": "Die abgelegte Datei ist nicht mehr da"}
    if src.exists():
        return 409, {"ok": False, "error": "Am ursprünglichen Ort liegt inzwischen etwas anderes"}
    try:
        shutil.move(str(final), str(src))          # erst die neue Datei weg, dann den Vorgänger zurück
        arch = e.get("archiviert")
        if arch and e.get("vorgaenger") and Path(arch).is_file() and not Path(e["vorgaenger"]).exists():
            shutil.move(arch, e["vorgaenger"])
    except OSError as exc:
        return 500, {"ok": False, "error": str(exc)}
    rueckgaengig_buchen(conn, token)
    return 200, {"ok": True, "src": str(src)}


# ── Projekt von Hand wählen, mehrere Dateien eines Drops ───────────────────────

def _datei_aus_vorgang(v: dict) -> DateiInfo:
    d = DateiInfo(v["dateiname"], v["groesse"], v["mtime"], v["hash"], None, None, v["host"])
    d.merkmale = v["merkmale"] or {}
    return d


def projekt_waehlen(conn, token: str, projekt_id: int, par=None) -> dict | None:
    """Der Nutzer wählt eines der angebotenen Projekte: Ordner-Vorschlag für dieses Projekt neu berechnen und
    speichern. Der Vorschlag danach gilt als „von Hand gewähltes Projekt" (p = 1)."""
    v = holen(conn, token)
    ctx = kontext.holen(conn)
    if v is None or v["status"] != "offen" or projekt_id not in ctx.projekte:
        return None
    vs = vorschlagen(ctx, _datei_aus_vorgang(v), par, projekt_id=projekt_id)
    vs["dateiname_vorschlag"] = (v["vorschlag"] or {}).get("dateiname_vorschlag")
    alt = v["vorschlag"] or {}
    vs["projekte_angeboten"] = alt.get("projekte_angeboten") or alt.get("projekte")
    with conn:
        conn.execute("UPDATE ablage_vorgang SET vorschlag = ? WHERE token = ?", (json.dumps(vs), token))
    return vs


def _stamm_gruppe(name: str) -> tuple:
    """Dateien „gehören zusammen", wenn Endung und die ersten zwei Wörter des Namens (ohne Datum) gleich sind."""
    k = mk.vorgaenger_name(name)
    ext = Path(k).suffix
    woerter = k[: -len(ext)].split() if ext else k.split()
    return ext, tuple(woerter[:2])


def gruppe_angleichen(conn, tokens: list[str], par=None) -> None:
    """Mehrfach-Drop (Auftrag §6.6). Alle Dateien sind einzeln bewertet. Dann: ist EINE beim Projekt eindeutig
    (≥ 0.95) und andere unklar, kommen die anderen in dieses Projekt; gleiche Endung und gleicher Namensanfang
    ergeben einen gemeinsamen Ordnervorschlag (der der ersten Datei, sofern deren Fall besser ist)."""
    if len(tokens) < 2:
        return
    vs_alle = [holen(conn, t) for t in tokens]
    vs_alle = [v for v in vs_alle if v and v["status"] == "offen"]
    if len(vs_alle) < 2 or any((v["vorschlag"] or {}).get("fall") == "vorbereitung" for v in vs_alle):
        return
    ctx = kontext.holen(conn)
    sicher = [v for v in vs_alle if (v["vorschlag"].get("projekte") or [{}])[0].get("p", 0) >= 0.95
              and v["vorschlag"]["fall"] != "duplikat"]
    if sicher:
        ziel = sicher[0]["vorschlag"]["projekte"][0]["id"]
        for v in vs_alle:
            vs = v["vorschlag"]
            if vs["fall"] == "projekt_unklar" and ziel in ctx.projekte and not vs.get("angeglichen"):
                neu = vorschlagen(ctx, _datei_aus_vorgang(v), par, projekt_id=ziel)
                neu["projekte"][0]["gruende"] = ["Projekt von einer anderen Datei dieses Drops übernommen"]
                neu["dateiname_vorschlag"] = vs.get("dateiname_vorschlag")
                neu["angeglichen"] = True
                neu["projekte_angeboten"] = vs.get("projekte")
                with conn:
                    conn.execute("UPDATE ablage_vorgang SET vorschlag = ? WHERE token = ?", (json.dumps(neu), v["token"]))
    rang = {"eindeutig": 0, "teilweise": 1, "ordner_unklar": 2, "projekt_unklar": 3, "duplikat": 4}
    gruppen: dict[tuple, list] = {}
    for t in tokens:
        v = holen(conn, t)
        if v and v["status"] == "offen" and v["vorschlag"]["fall"] in rang:
            gruppen.setdefault(_stamm_gruppe(v["dateiname"]), []).append(v)
    for mitglieder in gruppen.values():
        if len(mitglieder) < 2:
            continue
        erste = mitglieder[0]["vorschlag"]
        for v in mitglieder[1:]:
            vs = v["vorschlag"]
            gleiches_projekt = (vs.get("projekte") or [{}])[0].get("id") == (erste.get("projekte") or [{}])[0].get("id")
            if (gleiches_projekt and not vs.get("gruppe_angeglichen") and rang[vs["fall"]] > rang[erste["fall"]]
                    and erste["fall"] in ("eindeutig", "teilweise")):
                vs.update(fall=erste["fall"], sicher_bis=erste["sicher_bis"], optionen=erste["optionen"],
                          gruppe_angeglichen=True)
                with conn:
                    conn.execute("UPDATE ablage_vorgang SET vorschlag = ? WHERE token = ?", (json.dumps(vs), v["token"]))


def vorschlag_nachholen(conn, token: str, par=None) -> dict | None:
    """War der Kontext bei der Analyse noch nicht bereit (fall 'vorbereitung'), den Vorschlag jetzt rechnen — ohne
    zu blockieren. Gibt den (neuen) Vorschlag zurück oder None, solange noch geladen wird."""
    v = holen(conn, token)
    if v is None or (v["vorschlag"] or {}).get("fall") != "vorbereitung":
        return v["vorschlag"] if v else None
    ctx = kontext.holen_wartend(conn, warten_s=0.0)
    if ctx is None:
        return None
    d = _datei_aus_vorgang(v)
    vs = vorschlagen(ctx, d, par)
    if vs.get("projekte") and vs["projekte"][0]["p"] >= (par.projekt_sicher if par else 0.6) and vs["fall"] != "duplikat":
        try:
            from web.dashboard import _render_filename_template
            n = _render_filename_template(connection_setting("upload.filename_template") or "{dateiname}",
                                          project_name=vs["projekte"][0]["name"], original_name=v["dateiname"])
            vs["dateiname_vorschlag"] = None if n == v["dateiname"] else n
        except Exception:
            pass
    with conn:
        conn.execute("UPDATE ablage_vorgang SET vorschlag = ? WHERE token = ?", (json.dumps(vs), token))
    return vs
