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

CONFIG_PATH = Path(__file__).parent / "config.json"

# Lokaler HTTP-Server der Archivio-Helper-Menubar-App (siehe archivio_helper.py).
# Läuft auf derselben Station wie dieser MCP-Server; öffnet Dateien mit den
# Rechten des Helpers (Full Disk Access) — der von Claude Desktop gestartete
# MCP-Subprozess hätte die u.U. nicht.
HELPER_PORT = 44380


def _server_url() -> str:
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        return cfg.get("server_url", "http://localhost:8000").rstrip("/")
    except Exception:
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


mcp = FastMCP("archivio")


@mcp.tool()
def search(query: str, project: str = "", scope: str = "docs,filenames") -> str:
    """Durchsucht Archivio per Volltextsuche (Dokumente, Dateinamen, Mails, Ordner).

    query: Suchbegriff(e).
    project: optionale Projekt-ID zum Einschränken.
    scope: Komma-getrennt, z.B. "docs,filenames" oder "docs,filenames,folders".
    """
    base = _server_url()
    try:
        resp = requests.get(
            f"{base}/api/mcp/search",
            params={"q": query, "project_id": project, "search_in": scope, "limit": 20},
            timeout=15,
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
        loc     = r.get("filepath") or r.get("mail_sender") or ""
        excerpt = r.get("excerpt") or ""
        entry   = f"- {r['filename']} [{proj}]"
        if loc:
            entry += f"\n  Pfad: {loc}"
        if excerpt:
            entry += f"\n  Auszug: {excerpt}"
        lines.append(entry)
    for f in folders:
        lines.append(f"- \U0001F4C1 {f['name']} [{f.get('project_name') or '—'}]\n  Pfad: {f['path']}")

    return "\n".join(lines)


@mcp.tool()
def semantic_search(query: str, project: str = "") -> str:
    """Semantische Suche (KI-Suche) über Dokument-Inhalte — findet auch sinngemäße Treffer,
    die die Volltextsuche verpasst. Gibt Text-Auszüge zurück; die Antwort formuliert Claude selbst.

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

    lines = []
    for s in sources:
        proj = s.get("project_name") or "—"
        page = f", Seite {s['page_number']}" if s.get("page_number") else ""
        lines.append(
            f"- {s['filename']} [{proj}{page}] (Score {s.get('score', 0):.2f})\n"
            f"  Pfad: {s.get('filepath') or '—'}\n"
            f"  Inhalt: {(s.get('content') or '').strip()[:500]}"
        )
    return "\n".join(lines)


@mcp.tool()
def open_file(path: str) -> str:
    """Öffnet eine Datei aus Archivio in der zugehörigen App (z.B. Bild, PDF) auf dem Mac.

    Nutzt den lokalen Archivio Helper — funktioniert nur auf der Station, auf der
    Claude Desktop + Archivio Helper laufen und die die Datei erreichen kann.

    path: absoluter Dateipfad, exakt wie in den Suchergebnissen unter "Pfad" angegeben
          (z.B. "/Volumes/Groups/.../Attika_rechts_Event.jpg").
    """
    err = _helper_action("open", path)
    return err if err else f"Datei wird geöffnet: {path}"


@mcp.tool()
def reveal_file(path: str) -> str:
    """Zeigt eine Datei im Finder (markiert sie in ihrem Ordner) via Archivio Helper.

    path: absoluter Dateipfad wie in den Suchergebnissen unter "Pfad".
    """
    err = _helper_action("reveal", path)
    return err if err else f"Im Finder angezeigt: {path}"


if __name__ == "__main__":
    mcp.run()
