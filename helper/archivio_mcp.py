"""Archivio MCP-Server – read-only Zugriff auf die Volltext-/KI-Suche für Claude Desktop.

Läuft als stdio-Subprozess von Claude Desktop, mit dem im Archivio-Helper eingebetteten
Python. Ruft den zentralen Archivio-Server über HTTP im LAN auf (Server-URL aus derselben
config.json, die auch der Helper fürs Status-Menü nutzt — respektiert also automatisch,
wenn der Nutzer im Helper-Menü "Server ändern" eine andere URL einstellt).
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import requests
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

CONFIG_PATH = Path(__file__).parent / "config.json"

# Alle Archivio-Tools sind read-only (keine Aenderung an Dokumenten/DB) und arbeiten
# ausschliesslich auf dem lokalen NAS/Server, nicht "open world". Als Tool-Annotation
# mitgegeben, damit MCP-Clients (Claude Desktop/Claude.ai) das bei der Standard-
# Berechtigung beruecksichtigen koennen -- ohne das mussten Nutzer jedes Tool manuell
# in den Connector-Einstellungen von "Jedes Mal fragen" auf "Immer erlauben" umstellen.
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
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
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        url = (cfg.get("server_url") or "").rstrip("/")
        if url:
            return url
    except Exception:
        pass
    return "http://localhost:8000"


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
    semantic_search() nur nachschieben, wenn hier nichts Passendes dabei ist oder die Frage
    inhaltlich/sinngemäss ist statt nach einem bekannten Namen zu suchen.

    query: Suchbegriff(e).
    project: optionale Projekt-ID zum Einschränken.
    scope: Komma-getrennt aus "docs" (Dokumentinhalt), "filenames" (Dateinamen),
    "folders" (Ordnernamen) — standardmässig alle drei aktiv.

    Jeder Treffer hat eine [ID nnn]: mit read_document(nnn) den Volltext laden,
    mit open_file(pfad) die Datei extern öffnen.
    """
    base = _server_url()
    try:
        resp = requests.get(
            f"{base}/api/mcp/search",
            params={"q": query, "project_id": project, "search_in": scope, "limit": 20},
            timeout=40,  # Mehrwort-Queries mit vielen FTS-OR-Zweigen koennen auf grossen
                         # Indizes mehrere Sekunden dauern -- 15s war knapp bemessen.
        )
        resp.raise_for_status()
    except Exception as e:
        return f"Fehler beim Zugriff auf Archivio ({base}): {e}"

    data    = resp.json()
    results = data.get("results", [])
    folders = data.get("folders", [])
    if not results and not folders:
        return f"Keine Treffer für «{query}»."

    lines = []
    for r in results:
        proj    = r.get("project_name") or "—"
        excerpt = r.get("excerpt") or ""
        entry   = f"- [ID {r.get('id')}] {r['filename']} [{proj}]"
        if r.get("filepath"):
            entry += f"\n  Pfad: {r['filepath']}"
        elif r.get("mail_sender"):
            von = f"\n  Mail von: {r['mail_sender']}"
            if r.get("mail_date"):
                von += f" ({r['mail_date']})"
            entry += von
        if excerpt:
            entry += f"\n  Auszug: {excerpt}"
        lines.append(entry)
    for f in folders:
        lines.append(f"- \U0001F4C1 {f['name']} [{f.get('project_name') or '—'}]\n  Pfad: {f['path']}")

    return "\n".join(lines)


@mcp.tool(annotations=_READ_ONLY)
def semantic_search(query: str, project: str = "") -> str:
    """Semantische Suche (KI-Suche) über Dokument-Inhalte — findet auch sinngemäße Treffer,
    die die Volltextsuche verpasst (z.B. Umschreibungen, Synonyme, "worum geht es in..."-
    Fragen). Für bekannte Datei-/Ordnernamen oder exakte Fachbegriffe stattdessen zuerst
    search() nutzen, das ist dafür zuverlässiger. Gibt Text-Auszüge zurück; die Antwort
    formuliert Claude selbst.

    query: Frage oder Suchbegriff.
    project: optionale Projekt-ID zum Einschränken.
    """
    base = _server_url()
    try:
        resp = requests.get(
            f"{base}/api/mcp/semantic-search",
            params={"q": query, "project_id": project, "limit": 12},
            timeout=60,
        )
        resp.raise_for_status()
    except Exception as e:
        return f"Fehler beim Zugriff auf Archivio ({base}): {e}"

    data = resp.json()
    if data.get("ollama_missing"):
        return "Semantische Suche nicht verfügbar — Ollama läuft nicht auf dem Archivio-Server."
    sources = data.get("sources", [])
    if not sources:
        return data.get("error") or f"Keine relevanten Inhalte für «{query}» gefunden."

    # match_type erklärt, WIE der Treffer gefunden wurde — wichtig, damit der Score
    # nicht als exakte, über alle Treffer hinweg vergleichbare Zahl missverstanden wird
    # (Volltext-Treffer bekommen einen plausiblen Näherungswert, kein echtes Embedding-Mass).
    _MATCH_LABELS = {
        "heading":  "Überschrift/Norm-Definition, Volltext-Treffer",
        "fts":      "Volltext-Treffer",
        "like_and": "Volltext-Treffer (unscharf)",
        "like_or":  "Volltext-Treffer (unscharf, Teilbegriff)",
        "semantic": "semantischer Treffer",
    }
    lines = []
    for s in sources:
        proj  = s.get("project_name") or "—"
        page  = f", Seite {s['page_number']}" if s.get("page_number") else ""
        label = _MATCH_LABELS.get(s.get("match_type"), "")
        score_str = f"Score {s.get('score', 0):.2f}" + (f", {label}" if label else "")
        lines.append(
            f"- [ID {s.get('document_id')}] {s['filename']} [{proj}{page}] ({score_str})\n"
            f"  Pfad: {s.get('filepath') or '—'}\n"
            f"  Inhalt: {(s.get('content') or '').strip()[:500]}"
        )
    return "\n".join(lines)


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
    """
    base = _server_url()
    try:
        resp = requests.get(
            f"{base}/api/mcp/document", params={"document_id": document_id}, timeout=30
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
