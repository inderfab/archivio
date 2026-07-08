"""Archivio MCP-Server – read-only Zugriff auf die Volltext-/KI-Suche für Claude Desktop.

Läuft als stdio-Subprozess von Claude Desktop, mit dem im Archivio-Helper eingebetteten
Python. Ruft den zentralen Archivio-Server über HTTP im LAN auf (Server-URL aus derselben
config.json, die auch der Helper fürs Status-Menü nutzt — respektiert also automatisch,
wenn der Nutzer im Helper-Menü "Server ändern" eine andere URL einstellt).
"""
from __future__ import annotations

import json
from pathlib import Path

import requests
from mcp.server.fastmcp import FastMCP

CONFIG_PATH = Path(__file__).parent / "config.json"


def _server_url() -> str:
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        return cfg.get("server_url", "http://localhost:8000").rstrip("/")
    except Exception:
        return "http://localhost:8000"


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


if __name__ == "__main__":
    mcp.run()
