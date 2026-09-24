"""Protokoll aller Datenübermittlungen über die MCP-Schnittstelle.

Deckt nur die beiden Tools ab, die tatsächlich Dokumentinhalt an Claude senden
(search, document) -- open_file/reveal_file/list_folder öffnen
nur lokal eine Datei bzw. listen Ordnernamen, ohne dass Inhalt das Büro verlässt.

Wird ausschliesslich von web/api.py (mcp_search/mcp_document)
aufgerufen, direkt im Anschluss an scanner.norms.redact_hits()/guard_read() (und ab
der Sperrliste auch scanner.block_list) -- diese Aufrufer wissen bereits, was
tatsächlich übermittelt bzw. blockiert wurde, hier wird nur noch geschrieben.
"""
from __future__ import annotations

import json
import logging
import sqlite3

log = logging.getLogger(__name__)


def log_access(
    conn: sqlite3.Connection,
    tool: str,
    query: str,
    project_id: str | int | None,
    files: list[dict],
    blocked: list[dict],
    chars_sent: int = 0,
    error: str | None = None,
    session_id: str | None = None,
) -> None:
    """Schreibt eine Protokollzeile. files/blocked sind Listen schlanker Dicts
    (id/path/project/extension bzw. path-oder-filename/reason) -- roh als JSON
    abgelegt, die Seite /mcp-log formatiert sie erst beim Anzeigen. chars_sent wird
    vom Aufrufer übergeben (der kennt die tatsächlich übermittelten Auszüge/Inhalte,
    bevor sie zu schlanken Log-Einträgen reduziert werden). session_id kommt von
    helper/archivio_mcp.py (einmal pro Subprozess generiert) und erlaubt, mehrere
    Tool-Aufrufe derselben Claude-Verbindung in der Anzeige zusammenzufassen --
    siehe web/main.py::_group_mcp_log_entries()."""
    if error:
        status = "error"
    elif blocked and not files:
        status = "blocked"
    else:
        status = "ok"
    try:
        pid = int(project_id) if project_id else None
    except (TypeError, ValueError):
        # project_id kann ein nicht auflösbarer Projekt-Name/-Bezug sein (siehe
        # web/api.py::_resolve_mcp_project_ref) -- der Grund steht bereits in
        # blocked_json, hier nur nicht an der int-Spalte scheitern und den
        # gesamten Log-Eintrag verlieren.
        pid = None
    try:
        with conn:
            conn.execute(
                """INSERT INTO mcp_log
                   (tool, query, project_id, files_json, chars_sent, blocked_json, status, session_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tool,
                    query or None,
                    pid,
                    json.dumps(files, ensure_ascii=False),
                    chars_sent,
                    json.dumps(blocked, ensure_ascii=False),
                    status,
                    session_id or None,
                ),
            )
    except Exception as exc:
        # Das Protokoll darf eine MCP-Antwort nie verhindern -- ein Logging-Fehler
        # ist ein Beobachtungsproblem, kein Grund die eigentliche Anfrage abzubrechen.
        log.warning("MCP-Log fehlgeschlagen (tool=%s): %s", tool, exc)


def cleanup_old(conn: sqlite3.Connection, retention_days: int) -> int:
    """Löscht Protokollzeilen älter als retention_days. Gibt die Anzahl gelöschter
    Zeilen zurück (0 bei retention_days <= 0, was 'nie löschen' bedeuten würde --
    kein Aufrufer soll das versehentlich mit 'sofort alles löschen' verwechseln)."""
    if retention_days <= 0:
        return 0
    with conn:
        cur = conn.execute(
            "DELETE FROM mcp_log WHERE ts < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)",
            (f"-{retention_days} days",),
        )
        return cur.rowcount
