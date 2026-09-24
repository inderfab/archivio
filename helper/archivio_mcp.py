"""Archivio MCP-Server – read-only Zugriff auf die Volltextsuche für Claude Desktop.

Läuft als stdio-Subprozess von Claude Desktop, mit dem im Archivio-Helper eingebetteten
Python. Ruft den zentralen Archivio-Server über HTTP im LAN auf (Server-URL aus derselben
config.json, die auch der Helper fürs Status-Menü nutzt — respektiert also automatisch,
wenn der Nutzer im Helper-Menü "Server ändern" eine andere URL einstellt).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from urllib.parse import quote

import requests
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

# Gleicher Ort wie archivio_helper.py::CONFIG_PATH -- nutzer-schreibbar, NICHT das
# App-Bundle (schreibgeschützt, siehe dortiger Kommentar). Nur relevant als Fallback,
# falls der Helper gerade nicht erreichbar ist (Schritt 1 in _server_url() unten).
CONFIG_PATH = Path.home() / ".archivio" / "helper_config.json"
_BUNDLED_CONFIG_PATH = Path(__file__).parent / "config.json"

# Einmal pro Subprozess-Start erzeugt -- Claude Desktop hält diesen Subprozess
# für die Dauer der Verbindung am Leben, i.d.R. also eine ganze Unterhaltung lang.
# Wird bei jedem Tool-Aufruf an den Server mitgeschickt, damit /mcp-log mehrere
# Aufrufe derselben Verbindung zusammenfassen kann (eine Frage löst bei Claude oft
# mehrere Such-/Nachlade-Aufrufe aus, die sonst wie unabhängige Zugriffe wirken).
_SESSION_ID = uuid.uuid4().hex[:8]

# Alle Archivio-Tools sind read-only (keine Aenderung an Dokumenten/DB) und arbeiten
# ausschliesslich auf dem lokalen NAS/Server, nicht "open world". Als Tool-Annotation
# mitgegeben, damit MCP-Clients (Claude Desktop/Claude.ai) das bei der Standard-
# Berechtigung beruecksichtigen koennen -- ohne das mussten Nutzer jedes Tool manuell
# in den Connector-Einstellungen von "Jedes Mal fragen" auf "Immer erlauben" umstellen.
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
)

# merge_documents_to_pdf legt neu eine Datei in ~/Downloads an -- anders als die
# übrigen, rein lesenden Tools deshalb NICHT readOnlyHint=True (das würde MCP-Clients
# fälschlich sagen, der Aufruf verändere nichts auf der Platte). Nicht destruktiv
# (überschreibt/löscht keine bestehende Datei, jeder Aufruf bekommt einen eigenen
# Zeitstempel im Dateinamen) und nicht idempotent (zweimal aufgerufen entstehen zwei
# Dateien, kein Aktualisieren derselben).
_WRITES_NEW_FILE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False,
)

# Lokaler HTTP-Server der Archivio-Helper-Menubar-App (siehe archivio_helper.py).
# Läuft auf derselben Station wie dieser MCP-Server; öffnet Dateien mit den
# Rechten des Helpers (Full Disk Access) — der von Claude Desktop gestartete
# MCP-Subprozess hätte die u.U. nicht.
HELPER_PORT = 44380


def _server_url() -> str:
    """Ermittelt die Archivio-Server-URL. Reihenfolge:
    1. den laufenden Helper fragen (localhost:44380/config) — das ist die im
       Helper-Menü gesetzte, aktuelle URL (z.B. http://windows.local:8000);
    2. eigene config.json neben diesem Skript;
    3. Fallback localhost:8000.
    Der Helper ist der zuverlässigste Punkt, weil er nur EINE laufende Instanz pro
    Station hat und seine URL im Menü pflegt — eine evtl. veraltete gebündelte
    config.json führt so nicht mehr zum falschen Server.
    """
    try:
        resp = requests.get(f"http://localhost:{HELPER_PORT}/config", timeout=2)
        if resp.status_code == 200:
            url = (resp.json().get("server_url") or "").rstrip("/")
            if url:
                return url
    except Exception:
        pass
    for path in (CONFIG_PATH, _BUNDLED_CONFIG_PATH):
        try:
            cfg = json.loads(path.read_text())
            url = (cfg.get("server_url") or "").rstrip("/")
            if url:
                return url
        except Exception:
            continue
    return "http://localhost:8000"


def _archivio_link(path: str) -> str:
    """Baut einen 'Archivio-Link' -- dieselbe URL, die die Finder-Schnellaktion
    "Archivio-Link kopieren" erzeugt (siehe helper/ArchivioLink.workflow). Zeigt auf
    den LOKALEN Helper-Server (localhost, nicht den Archivio-Server), läuft also
    exakt auf der Station, auf der Claude Desktop selbst läuft. Ein Klick öffnet die
    Datei direkt im Finder/der zugehörigen App -- ohne Rückfrage an Claude und ohne
    die Projektordner-Prüfung von open_file()/reveal_file(), die bei dieser Nutzung
    (der Nutzer klickt selbst, kein Tool-Aufruf) nur eine unnötige Fehlerquelle wäre."""
    return f"http://localhost:{HELPER_PORT}/link?path={quote(path, safe='')}"


def _archivio_link_markdown(path: str) -> str:
    """Fertig formatierter Markdown-Link -- wird UNVERÄNDERT in die Werkzeug-Antwort
    eingebettet, damit Claude ihn nur noch übernehmen statt selbst formatieren muss.
    Grund: in der Praxis hat Claude den blossen Link teils in einen Codeblock gesetzt
    (dort nicht klickbar) oder sich einen eigenen file://-Link gebaut (unzuverlässig,
    z.B. bei Leerzeichen/Sonderzeichen im Pfad) statt diesen zu verwenden -- ein
    bereits vollständiges Markdown-Link-Snippet lässt dafür keinen Interpretations-
    spielraum mehr."""
    return f"[📂 Im Finder öffnen]({_archivio_link(path)})"


_STATUS_LABELS = {"aktuell": "Aktuell", "veraltet": "Veraltet ⚠️", "ungeprüft": "Ungeprüft"}


def _norm_meta_line(r: dict) -> str:
    """Gültigkeitsdatum + Aktualitäts-Status einer als Norm erkannten Datei --
    reine Metadaten (siehe scanner/norms.py Modul-Docstring), unabhängig von der
    Inhaltssperre. Leerer String, wenn nichts davon bekannt ist (z.B. kein
    Gültigkeitsdatum extrahierbar gewesen, oder noch nie online geprüft)."""
    if not r.get("is_norm"):
        return ""
    parts = []
    if r.get("norm_valid_from"):
        parts.append(f"\n  Gültig ab: {r['norm_valid_from']}")
    status = r.get("norm_check_status")
    if status and status != "ungeprüft":
        parts.append(f"\n  Aktualität: {_STATUS_LABELS.get(status, status)}")
    return "".join(parts)


def _helper_action(action: str, path: str) -> str:
    """Ruft /open bzw. /reveal des lokalen Helper-Servers auf und liefert eine
    menschenlesbare Statusmeldung zurück."""
    url = f"http://localhost:{HELPER_PORT}/{action}?path={quote(path, safe='')}"
    try:
        resp = requests.get(url, timeout=10)
    except Exception as e:
        return (
            f"Archivio Helper nicht erreichbar (localhost:{HELPER_PORT}): {e}. "
            "Läuft die «Archivio Helper»-App in der Menüleiste?"
        )
    if resp.status_code == 200:
        return None  # Erfolg — Aufrufer formuliert die Meldung
    if resp.status_code == 404:
        return f"Datei nicht gefunden: {path}"
    return f"Helper-Fehler ({resp.status_code}) bei «{action}» für {path}"


def _allowed_base_folders() -> list[str] | None:
    """Holt die erlaubten NAS-Wurzelpfade vom Server. None bei Fehler (Aufrufer muss
    dann ablehnen, nicht offen lassen -- kein Fallback auf "alles erlaubt")."""
    base = _server_url()
    try:
        resp = requests.get(f"{base}/api/mcp/base-folders", timeout=5)
        resp.raise_for_status()
        return resp.json().get("folders", [])
    except Exception:
        return None


def _check_path_allowed(path: str) -> tuple[Path | None, str | None]:
    """Prüft, ob `path` innerhalb eines konfigurierten Archivio-Projektordners liegt --
    gemeinsame Sicherheitsgrenze für JEDES Tool, das eine Datei/einen Ordner ausserhalb
    des MCP-Suchindex direkt anfasst (list_folder, open_file, reveal_file). Gibt bei
    Erfolg (aufgelöster Pfad, None) zurück, sonst (None, Fehlermeldung) -- der Aufrufer
    gibt die Fehlermeldung dann direkt als Tool-Ergebnis zurück, statt fortzufahren."""
    allowed = _allowed_base_folders()
    if allowed is None:
        return None, f"Konnte erlaubte Ordner nicht vom Archivio-Server abfragen ({_server_url()})."
    if not allowed:
        return None, "Keine NAS-Ordner in Archivio konfiguriert."
    try:
        target = Path(path).resolve()
    except Exception as e:
        return None, f"Ungültiger Pfad «{path}»: {e}"
    if not any(target == Path(a).resolve() or Path(a).resolve() in target.parents
               for a in allowed):
        return None, f"Pfad ausserhalb der erlaubten Archivio-Ordner: {path}"
    return target, None


mcp = FastMCP("archivio")


@mcp.tool(annotations=_READ_ONLY)
def search(query: str, project: str = "", scope: str = "docs,filenames,folders") -> str:
    """Durchsucht Archivio per Volltextsuche (Dokumente, Dateinamen, Mails, Ordnernamen).

    IMMER dieses Tool zuerst versuchen, wenn ein Dateiname, Ordnername, Aktenzeichen,
    BKP-Nummer oder ein exakter Fachbegriff bekannt/vermutet wird — auch wenn der Begriff
    nur im Datei- oder Ordnernamen steht (nicht im Dokumentinhalt), findet dieses Tool ihn.
    Bei einer inhaltlichen Frage ohne bekannten Namen mit den wahrscheinlichen
    Fachbegriffen suchen und die Anfrage bei Bedarf mit anderen Begriffen wiederholen.

    query: Suchbegriff(e).
    project: optional, zum Einschränken auf ein Projekt -- den Projektnamen genau so
    übernehmen, wie er in eckigen Klammern [...] bei einem vorherigen Treffer stand
    (z.B. "211 Emmenhof Derendingen" oder auch nur "Emmenhof"), keine ID erfinden.
    scope: Komma-getrennt aus "docs" (Dokumentinhalt), "filenames" (Dateinamen),
    "folders" (Ordnernamen) — standardmässig alle drei aktiv.

    Jeder Treffer hat eine [ID nnn]: mit read_document(nnn) den Volltext laden,
    mit open_file(pfad) die Datei extern öffnen. Für den Nutzer zum Selbst-Anklicken
    steht ausserdem eine fertige Markdown-Link-Zeile "[📂 Im Finder öffnen](...)"
    dabei -- diese Zeile UNVERÄNDERT (Zeichen für Zeichen) in die Antwort
    übernehmen, NICHT in einen Codeblock setzen und NICHT selbst einen Link aus
    dem Pfad bauen (z.B. file://) -- nur das fertige Snippet ist in Claude Desktop
    zuverlässig klickbar.
    """
    base = _server_url()
    try:
        resp = requests.get(
            f"{base}/api/mcp/search",
            params={"q": query, "project_id": project, "search_in": scope, "limit": 20,
                    "session_id": _SESSION_ID},
            timeout=40,  # Mehrwort-Queries mit vielen FTS-OR-Zweigen koennen auf grossen
                         # Indizes mehrere Sekunden dauern -- 15s war knapp bemessen.
        )
        resp.raise_for_status()
    except Exception as e:
        return f"Fehler beim Zugriff auf Archivio ({base}): {e}"

    data    = resp.json()
    results = data.get("results", [])
    folders = data.get("folders", [])
    # notice: Anfrage sieht nach einer Normnummer aus (z.B. "SIA 400"/"SIA 416") --
    # unabhängig davon, ob die Suche sonst leer ausgeht. Praxisfall: "SIA 416" findet
    # oft nur beiläufige Arbeitsdokumente, die die Norm bloss ERWÄHNEN (Berechnungen,
    # Pläne), nicht die echte, offiziell abgelegte Norm -- der Fundort-Hinweis muss
    # deshalb AUCH bei vorhandenen Treffern angehängt werden, nicht nur beim
    # kompletten Leerlauf.
    notice = data.get("notice")
    if not results and not folders:
        if notice:
            return notice
        return f"Keine Treffer für «{query}»."

    lines = []
    for r in results:
        proj    = r.get("project_name") or "—"
        excerpt = r.get("excerpt") or ""
        entry   = f"- [ID {r.get('id')}] {r['filename']} [{proj}]"
        if r.get("filepath"):
            entry += f"\n  Pfad: {r['filepath']}"
            entry += _norm_meta_line(r)
            entry += f"\n  {_archivio_link_markdown(r['filepath'])}"
        elif r.get("mail_sender"):
            von = f"\n  Mail von: {r['mail_sender']}"
            if r.get("mail_date"):
                von += f" ({r['mail_date']})"
            entry += von
        if excerpt:
            entry += f"\n  Auszug: {excerpt}"
        lines.append(entry)
    for f in folders:
        lines.append(
            f"- \U0001F4C1 {f['name']} [{f.get('project_name') or '—'}]\n  Pfad: {f['path']}"
            f"\n  {_archivio_link_markdown(f['path'])}"
        )

    output = "\n".join(lines)
    if notice:
        output += f"\n\n---\n{notice}"
    return output


_READ_DOCUMENT_BLOCK_SIZE = 8000


@mcp.tool(annotations=_READ_ONLY)
def read_document(document_id: int, offset: int = 0) -> str:
    """Lädt den extrahierten Text-Inhalt eines Dokuments blockweise in die Unterhaltung —
    damit Claude ihn direkt lesen, zusammenfassen, umschreiben, übersetzen oder Fragen dazu
    beantworten kann.

    Für Text-Dokumente: Mails, PDFs, Word, Textdateien. Für Bilder/Pläne stattdessen
    open_file benutzen (die öffnet die Datei extern zum Anschauen).

    Liefert jeweils EINEN Textblock. Ist das Dokument länger, endet die Antwort mit einem
    Hinweis inkl. des offsets für den nächsten Block — falls die gesuchte Information im
    ersten Block nicht dabei ist, read_document mit diesem offset erneut aufrufen, um
    weiterzulesen (statt anzunehmen, das Dokument sei bereits vollständig gelesen).

    document_id: die Zahl aus der [ID nnn] eines Suchergebnisses.
    offset: Zeichen-Position, ab der gelesen werden soll (0 = Anfang, Standard).

    Enthält eine fertige Markdown-Link-Zeile "[📂 Im Finder öffnen](...)" -- diese
    UNVERÄNDERT übernehmen, nicht in einen Codeblock setzen und nicht selbst einen
    Link aus dem Pfad bauen.
    """
    base = _server_url()
    try:
        resp = requests.get(
            f"{base}/api/mcp/document",
            params={"document_id": document_id, "session_id": _SESSION_ID}, timeout=30,
        )
    except Exception as e:
        return f"Fehler beim Zugriff auf Archivio ({base}): {e}"
    if resp.status_code == 404:
        return f"Kein Dokument mit ID {document_id} gefunden."
    if resp.status_code != 200:
        return f"Archivio-Fehler ({resp.status_code}) für Dokument {document_id}."

    d       = resp.json()
    header  = [f"Datei: {d.get('filename')}"]
    if d.get("filepath"):
        header.append(f"Pfad: {d['filepath']}")
        header.append(_archivio_link_markdown(d['filepath']))
    mail = d.get("mail")
    if mail:
        header.append(f"Von: {mail.get('sender', '')}")
        header.append(f"An: {mail.get('recipients', '')}")
        if mail.get("cc"):
            header.append(f"Cc: {mail['cc']}")
        header.append(f"Betreff: {mail.get('subject', '')}")
        header.append(f"Datum: {mail.get('date', '')}")

    content = (d.get("content") or "").strip()
    if not content:
        return (
            "\n".join(header)
            + "\n\n(Kein extrahierter Text vorhanden — evtl. ein Bild/Plan ohne Text. "
            "Zum Anschauen open_file benutzen.)"
        )

    total = len(content)
    if offset >= total:
        return f"Kein weiterer Inhalt ab Zeichen {offset} (Dokument hat {total} Zeichen)."

    block       = content[offset:offset + _READ_DOCUMENT_BLOCK_SIZE]
    next_offset = offset + len(block)
    footer = ""
    if next_offset < total:
        footer = (
            f"\n\n… (Zeichen {offset}–{next_offset} von {total} — weiterer Inhalt vorhanden, "
            f"bei Bedarf read_document({document_id}, offset={next_offset}) aufrufen)"
        )

    prefix = "\n".join(header) + "\n\n" if offset == 0 else ""
    return prefix + block + footer


@mcp.tool(annotations=_WRITES_NEW_FILE)
def merge_documents_to_pdf(document_ids: str, title: str = "Zusammengeführte Dokumente") -> str:
    """Führt mehrere über search() gefundene PDF-Dokumente zu EINER
    neuen PDF-Datei zusammen -- z.B. wenn alle Materialblätter aus mehreren Projekten
    gesucht und danach als eine gemeinsame Datei gewünscht wurden. Claude kann PDFs
    nicht selbst zusammenführen (kein Dateizugriff über MCP); Archivio erledigt das
    serverseitig, das Ergebnis landet lokal im Downloads-Ordner dieses Rechners.

    document_ids: kommagetrennte Liste der [ID nnn]-Werte aus vorherigen
    search()-Treffern, z.B. "142,891,203". Dokumente ohne
    Leserecht (nicht freigegebenes Projekt, erkannte Norm, Sperrliste) oder die
    kein PDF sind, werden übersprungen und im Ergebnis einzeln mit Grund gemeldet,
    nicht stillschweigend weggelassen.
    title: kurzer, sprechender Name für die neue Datei (ohne Dateiendung) -- passend
    zum Inhalt wählen, z.B. "Materialblätter Vergleich".
    """
    base = _server_url()
    try:
        resp = requests.get(
            f"{base}/api/mcp/merge-pdf",
            params={"document_ids": document_ids, "session_id": _SESSION_ID},
            timeout=60,
        )
    except Exception as e:
        return f"Fehler beim Zugriff auf Archivio ({base}): {e}"
    if resp.status_code not in (200, 400):
        return f"Archivio-Fehler ({resp.status_code}) beim Zusammenführen."

    data = resp.json()
    if not data.get("ok"):
        lines = [data.get("error") or "PDF-Zusammenführung fehlgeschlagen."]
        for s in data.get("skipped", []):
            lines.append(f"- {s.get('filename')}: {s.get('reason')}")
        return "\n".join(lines)

    import base64
    import re
    import time

    safe_title = re.sub(r"[^\w\-äöüÄÖÜ ]", "", title).strip() or "Zusammengeführte Dokumente"
    filename   = f"{safe_title}_{time.strftime('%Y%m%d-%H%M%S')}.pdf"
    out_path   = Path.home() / "Downloads" / filename
    try:
        out_path.write_bytes(base64.b64decode(data["pdf_base64"]))
    except Exception as e:
        return f"PDF wurde erstellt, konnte aber nicht gespeichert werden: {e}"

    merged = data["merged"]
    lines  = [
        f"✓ {merged} Dokument{'e' if merged != 1 else ''} zu „{filename}“ zusammengeführt.",
        f"Pfad: {out_path}",
        _archivio_link_markdown(str(out_path)),
    ]
    if data.get("skipped"):
        lines.append("\nÜbersprungen:")
        for s in data["skipped"]:
            lines.append(f"- {s.get('filename')}: {s.get('reason')}")
    return "\n".join(lines)


@mcp.tool(annotations=_READ_ONLY)
def open_file(path: str) -> str:
    """Öffnet eine Datei aus Archivio in der zugehörigen App (z.B. Bild, PDF) auf dem Mac.

    Nutzt den lokalen Archivio Helper — funktioniert nur auf der Station, auf der
    Claude Desktop + Archivio Helper laufen und die die Datei erreichen kann. Aus
    Sicherheitsgründen nur innerhalb der konfigurierten Archivio-Projektordner.

    path: absoluter Dateipfad, exakt wie in den Suchergebnissen unter "Pfad" angegeben
          (z.B. "/Volumes/Groups/.../Attika_rechts_Event.jpg").
    """
    _, guard_err = _check_path_allowed(path)
    if guard_err:
        return guard_err
    err = _helper_action("open", path)
    return err if err else f"Datei wird geöffnet: {path}"


@mcp.tool(annotations=_READ_ONLY)
def reveal_file(path: str) -> str:
    """Zeigt eine Datei im Finder (markiert sie in ihrem Ordner) via Archivio Helper.
    Aus Sicherheitsgründen nur innerhalb der konfigurierten Archivio-Projektordner.

    path: absoluter Dateipfad wie in den Suchergebnissen unter "Pfad".
    """
    _, guard_err = _check_path_allowed(path)
    if guard_err:
        return guard_err
    err = _helper_action("reveal", path)
    return err if err else f"Im Finder angezeigt: {path}"


@mcp.tool(annotations=_READ_ONLY)
def list_folder(path: str) -> str:
    """Listet den Inhalt eines Ordners (Unterordner + Dateien) — nützlich wenn der
    ungefähre Speicherort bekannt ist (z.B. aus einem vorherigen Suchtreffer), aber der
    genaue Dateiname nicht. Läuft direkt auf dieser Station (derselbe NAS-Zugriff wie der
    Archivio-Server), aus Sicherheitsgründen nur innerhalb der konfigurierten
    Archivio-Projektordner.

    path: absoluter Ordnerpfad, z.B. aus dem "Pfad"-Feld eines Suchtreffers (Elternordner).
    """
    target, guard_err = _check_path_allowed(path)
    if guard_err:
        return guard_err
    if not target.exists():
        return f"Ordner nicht gefunden: {path}"
    if not target.is_dir():
        return f"Kein Ordner (sondern eine Datei): {path}"

    try:
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except Exception as e:
        return f"Ordner konnte nicht gelesen werden ({e}): {path}"

    if not entries:
        return f"Ordner ist leer: {path}"

    lines = [f"Inhalt von {path}:"]
    for entry in entries:
        try:
            if entry.is_dir():
                lines.append(f"- 📁 {entry.name}/")
            else:
                size_kb = entry.stat().st_size / 1024
                lines.append(f"- {entry.name}  ({size_kb:.0f} KB)")
        except Exception:
            lines.append(f"- {entry.name} (nicht lesbar)")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
