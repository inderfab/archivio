"""Transport und Ausführung des Datei-Drops zwischen Helper (Mitarbeiter-Mac) und Server.

Bewusst ohne rumps/AppKit, damit Helper UND Server dieselbe Logik nutzen und sie testbar bleibt.

Ablauf (planung/datei-drop-ablage.md §3):
  Helper: Drop → Hash + (kleine Textformate) Upload → POST /api/ablage/analyse → Token merken
  Browser: Vorschlag bestätigen → Server speichert den Entscheid
  Helper: `ausfuehren(token)` holt dest/dateiname VOM SERVER und verschiebt/kopiert src → dest

Sicherheit: Die lokale Bridge antwortet mit `Access-Control-Allow-Origin: *`, jede Webseite könnte sie also
ansprechen. `ausfuehren` nimmt deshalb NUR einen Token. `src` kommt aus der Registry, die der Helper selbst
im Speicher führt (nur für selbst registrierte Dateien), `dest` vom Server, der geprüft hat, dass es in einem
Projekt liegt. Der Helper prüft das nochmals (`ist_unterhalb`) und lässt nie Pfade ausserhalb des Projekts zu.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import socket
import threading
import time
from pathlib import Path

MAX_HASH_BYTES = 200 * 1024 * 1024      # darüber nur Metadaten (kein Hash)
MAX_UPLOAD_BYTES = 50 * 1024 * 1024     # darüber (oder bei anderen Formaten) nur Metadaten
TOKEN_TTL_S = 24 * 3600
# Der Helper hat keine PDF-Bibliotheken und soll keine bekommen: Textformate gehen zum Server, CAD/Bilder
# (.pln, .dwg, .ifc …) nicht. Für sie reichen Name, Grösse, mtime und Hash.
UPLOAD_ENDUNGEN = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".txt", ".rtf", ".eml", ".odt", ".ods",
                   ".csv", ".md", ".pptx"}


def trocken() -> bool:
    return os.environ.get("ARCHIVIO_UPLOAD_DRY_RUN") == "1"


# ── Token-Registry (nur im Speicher des Helpers) ───────────────────────────────

class TokenRegistry:
    """Token → selbst registrierte Quelldatei. Nur was hier steht, darf ausgeführt werden."""

    def __init__(self, ttl_s: int = TOKEN_TTL_S):
        self._d: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_s

    def add(self, token: str, src: str, name: str) -> None:
        with self._lock:
            self._d[token] = {"src": src, "name": name, "ts": time.time()}

    def get(self, token) -> dict | None:
        if not isinstance(token, str) or not token:
            return None
        with self._lock:
            e = self._d.get(token)
            if e and time.time() - e["ts"] > self._ttl:
                self._d.pop(token, None)
                return None
            return dict(e) if e else None

    def entfernen(self, token: str) -> None:
        with self._lock:
            self._d.pop(token, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._d)


# ── Pfade ──────────────────────────────────────────────────────────────────────

def ist_unterhalb(pfad: str, basis: str) -> bool:
    """`pfad` liegt in `basis` (oder ist es), ohne `..` und ohne Umwege. Reiner Textvergleich auf
    Pfadkomponenten: '/x/Projekte2' liegt NICHT unter '/x/Projekte'."""
    if not pfad or not basis or not os.path.isabs(pfad) or not os.path.isabs(basis):
        return False
    if ".." in Path(pfad).parts or ".." in Path(basis).parts:
        return False
    import unicodedata
    p = unicodedata.normalize("NFC", os.path.normpath(pfad))
    b = unicodedata.normalize("NFC", os.path.normpath(basis))
    return p == b or p.startswith(b.rstrip("/") + "/")


def dateiname_ok(name: str) -> bool:
    return bool(name) and name == name.strip() and "/" not in name and "\x00" not in name \
        and name not in (".", "..") and not name.startswith(".")


def eindeutiger_name(ordner: Path, dateiname: str) -> Path:
    """Namenskollision → `name (2).ext` statt Überschreiben."""
    ziel = ordner / dateiname
    if not ziel.exists():
        return ziel
    stem, suffix = Path(dateiname).stem, Path(dateiname).suffix
    i = 2
    while True:
        kandidat = ordner / f"{stem} ({i}){suffix}"
        if not kandidat.exists():
            return kandidat
        i += 1


# ── Dateioperationen (Helper UND Server-Mac) ───────────────────────────────────

def datei_ablegen(src: str, dest_dir: str, dateiname: str, kopie: bool = False, modus: str = "behalten",
                  vorgaenger: str | None = None, archiv_ordner: str | None = None,
                  nur_planen: bool = False) -> dict:
    """Legt `src` nach `dest_dir/dateiname` ab. Rückgabe: {final, archiviert, ersetzt}.

    modus (nur wenn im Ordner schon eine gleichnamige Datei / ein Vorgänger liegt):
      archivieren   alter Stand → Archiv-Ordner (`archiv_ordner`, sonst `z_Archiv` neben dem alten Stand)
      ueberschreiben nur bei exakt gleichem Namen: alter Stand wird ersetzt
      behalten      beide bleiben, die neue Datei bekommt `(2)`
    Nichts wird je gelöscht ausser ausdrücklich überschrieben. `nur_planen` verändert nichts (Trockenlauf).
    """
    quelle = Path(src)
    ordner = Path(dest_dir)
    if not quelle.is_file():
        raise FileNotFoundError(f"Ausgangsdatei nicht gefunden: {src}")
    if not dateiname_ok(dateiname):
        raise ValueError(f"Ungültiger Dateiname: {dateiname!r}")
    if not ordner.is_dir():
        if ordner.parent.is_dir():          # neu anzulegender Ordner (Vorlage), aber nur eine Ebene
            if not nur_planen:
                ordner.mkdir()
        else:
            raise FileNotFoundError(f"Zielordner nicht gefunden: {dest_dir}")

    ergebnis = {"final": None, "archiviert": None, "ersetzt": None}
    ziel = ordner / dateiname
    alt = Path(vorgaenger) if vorgaenger and Path(vorgaenger).is_file() else None

    if modus == "archivieren" and alt is not None:
        archiv = Path(archiv_ordner) if archiv_ordner else alt.parent / "z_Archiv"
        if not archiv.is_dir() and archiv.parent.is_dir() and not nur_planen:
            archiv.mkdir()                                  # z_Archiv neben dem alten Stand anlegen
        ziel_archiv = eindeutiger_name(archiv, alt.name) if archiv.is_dir() else archiv / alt.name
        ergebnis["archiviert"] = str(ziel_archiv)
        if not nur_planen:
            shutil.move(str(alt), str(ziel_archiv))
    elif modus == "ueberschreiben" and ziel.exists():
        ergebnis["ersetzt"] = str(ziel)
        if nur_planen:
            ergebnis["final"] = str(ziel)
            return ergebnis
        tmp = ordner / f".{dateiname}.archivio-neu"
        shutil.copy2(quelle, tmp)
        os.replace(tmp, ziel)                # atomar: nie eine halb geschriebene Zieldatei
        if not kopie:
            quelle.unlink()
        ergebnis["final"] = str(ziel)
        return ergebnis

    final = eindeutiger_name(ordner, dateiname) if ordner.is_dir() else ziel
    ergebnis["final"] = str(final)
    if nur_planen:
        return ergebnis
    if kopie:
        shutil.copy2(quelle, final)
    else:
        shutil.move(str(quelle), str(final))
    return ergebnis


# ── Hash und Analyse-Upload ────────────────────────────────────────────────────

def sha256_datei(pfad: str, limit: int = MAX_HASH_BYTES) -> str | None:
    p = Path(pfad)
    try:
        if p.stat().st_size > limit:
            return None
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def dateien_analysieren(paths: list[str], server_url: str, registry: TokenRegistry, session=None,
                        host: str | None = None, log=None, timeout: int = 90) -> list[dict]:
    """Je Datei: Hash, (kleine Textformate) Upload, Analyse beim Server, Token merken.
    Rückgabe: [{token, name} | {name, fehler}] in Reihenfolge der Dateien."""
    import requests
    session = session or requests
    host = host or socket.gethostname()
    res = []
    for pfad in paths:
        p = Path(pfad)
        name = p.name
        try:
            st = p.stat()
            if not p.is_file():
                res.append({"name": name, "fehler": "Keine Datei (Ordner werden nicht abgelegt)"})
                continue
            daten = {"name": name, "size": str(st.st_size), "host": host,
                     "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime))}
            h = sha256_datei(pfad)
            if h:
                daten["hash"] = h
            files = None
            if p.suffix.lower() in UPLOAD_ENDUNGEN and st.st_size <= MAX_UPLOAD_BYTES:
                with open(p, "rb") as f:
                    r = session.post(f"{server_url.rstrip('/')}/api/ablage/analyse", data=daten,
                                     files={"datei": (name, f)}, timeout=timeout)
            else:
                r = session.post(f"{server_url.rstrip('/')}/api/ablage/analyse", data=daten, timeout=timeout)
            if r.status_code != 200:
                res.append({"name": name, "fehler": f"Server antwortet mit {r.status_code}"})
                continue
            token = r.json().get("token")
            if not token:
                res.append({"name": name, "fehler": "Server lieferte keinen Token"})
                continue
            registry.add(token, str(p), name)
            res.append({"token": token, "name": name})
        except Exception as exc:
            if log:
                log.warning("Analyse von %s fehlgeschlagen: %s", name, exc)
            res.append({"name": name, "fehler": str(exc)})
    return res


def ablage_url(server_url: str, tokens: list[str]) -> str:
    return f"{server_url.rstrip('/')}/dashboard/ablage?t={','.join(tokens)}"


# ── Ausführung (Bridge-Endpunkt /ablage/ausfuehren) ────────────────────────────

def ausfuehren(token, registry: TokenRegistry, server_url: str, session=None, log=None,
               dry_run: bool | None = None) -> tuple[int, dict]:
    """Führt eine vom Nutzer bestätigte Ablage aus. Rückgabe (HTTP-Status, JSON).

    Nimmt NUR den Token entgegen: unbekannter Token → 403; der Entscheid kommt vom Server und muss in einem
    Projekt liegen; Quelle kommt aus der Registry. Trockenlauf (`ARCHIVIO_UPLOAD_DRY_RUN=1`) verschiebt nichts.
    """
    import requests
    session = session or requests
    dry = trocken() if dry_run is None else dry_run
    eintrag = registry.get(token)
    if eintrag is None:
        return 403, {"ok": False, "error": "Unbekannter Token"}
    basis = server_url.rstrip("/")
    try:
        r = session.get(f"{basis}/api/ablage/{token}/entscheid", timeout=30)
    except Exception as exc:
        return 502, {"ok": False, "error": f"Server nicht erreichbar: {exc}"}
    if r.status_code != 200:
        return 409 if r.status_code in (404, 409) else 502, {"ok": False, "error": f"Server: {r.status_code}"}
    d = r.json()
    if d.get("status") != "bestaetigt":
        return 409, {"ok": False, "error": "Ablage ist nicht bestätigt"}
    projekt, dest = d.get("projekt_pfad"), d.get("dest")
    if not ist_unterhalb(dest, projekt):
        if log:
            log.warning("Ablage abgelehnt: %r liegt nicht in %r", dest, projekt)
        return 403, {"ok": False, "error": "Zielordner liegt ausserhalb des Projekts"}
    for extra in (d.get("vorgaenger"), d.get("archiv_ordner")):
        if extra and not ist_unterhalb(extra, projekt):
            return 403, {"ok": False, "error": "Vorgänger/Archiv liegt ausserhalb des Projekts"}
    if not dateiname_ok(d.get("dateiname") or ""):
        return 400, {"ok": False, "error": "Ungültiger Dateiname"}
    try:
        erg = datei_ablegen(eintrag["src"], dest, d["dateiname"], bool(d.get("kopie")),
                            d.get("vorgaenger_modus") or "behalten", d.get("vorgaenger"),
                            d.get("archiv_ordner"), nur_planen=dry)
    except FileNotFoundError as exc:
        return 404, {"ok": False, "error": str(exc)}
    except (OSError, ValueError) as exc:
        if log:
            log.warning("Ablage fehlgeschlagen: %s", exc)
        return 500, {"ok": False, "error": str(exc)}
    try:
        session.post(f"{basis}/api/ablage/{token}/abgelegt",
                     json={"final_path": erg["final"], "trocken": dry, "archiviert": erg["archiviert"]}, timeout=30)
    except Exception as exc:
        if log:
            log.warning("Meldung 'abgelegt' fehlgeschlagen: %s", exc)
    if not dry:
        registry.entfernen(token)
    if log:
        log.info("Ablage %s: %s -> %s", "(TROCKENLAUF)" if dry else "ausgeführt", eintrag["name"], erg["final"])
    return 200, {"ok": True, "final_path": erg["final"], "trocken": dry, "archiviert": erg["archiviert"],
                 "ersetzt": erg["ersetzt"]}
