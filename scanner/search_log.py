"""Protokoll jeder Websuche (normale Suche und KI-Suche) -- Anfrage, Trefferzahl,
Dauer und ob danach tatsächlich ein Treffer geöffnet wurde. Bewusst OHNE Bezug zu
Person oder Gerät (anders als scanner/mcp_log.py, dessen Zweck gerade die
Nachvollziehbarkeit ist) -- dient hier ausschliesslich dazu, Muster in der
Suchqualität sichtbar zu machen, z.B. häufige Anfragen ohne Treffer oder ohne
Klick. Grundlage für den Abschnitt "Suche-Protokoll" auf /system-status.

Wird von web/main.py (search/search_ai) aufgerufen.
"""
from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)


_COALESCE_WINDOW_S = 10


def log_search(
    conn: sqlite3.Connection,
    kind: str,
    query: str,
    project_id: int | None,
    filters: str,
    result_count: int,
    duration_ms: int,
    token: str | None = None,
    query_string: str | None = None,
) -> int | None:
    """Schreibt eine Protokollzeile, gibt deren id zurück -- die braucht der
    Aufrufer, um sie den gerade angezeigten Ergebnissen mitzugeben, damit ein
    späterer Klick (siehe log_click()) dieser Suche zugeordnet werden kann.
    None bei Fehlschlag: ein Logging-Fehler darf die eigentliche Suche nie
    verhindern, der Aufrufer zeigt die Ergebnisse dann einfach ohne Klick-Tracking.

    token (siehe index.html #search_token, ein zufälliger Wert pro Seitenaufruf):
    die Live-Suche feuert bei jedem Tastenanschlag (300ms Debounce) -- ohne
    Zusammenfassen würde ein einziges eingetipptes Wort mehrere Zeilen erzeugen
    ("ne", "net", "netzwerk"), was das Protokoll für die eigentliche Auswertung
    (häufige Anfragen ohne Treffer/Klick) unbrauchbar verwässert. Findet sich zum
    selben token eine noch unangeklickte (clicks=0) Zeile aus den letzten
    _COALESCE_WINDOW_S Sekunden, wird SIE aktualisiert statt eine neue anzulegen
    -- am Ende eines Tippvorgangs bleibt so nur die zuletzt eingetippte, "fertige"
    Anfrage übrig. Ein Klick markiert die Zeile implizit als abgeschlossen
    (clicks>0 sperrt sie fürs Zusammenfassen), ein längeres Zeitfenster ebenso --
    beides zusammen genügt, ohne dass der Client explizit "fertig getippt"
    signalisieren müsste.

    query_string (der rohe Query-String der /search-Anfrage):
    erlaubt der "Resultate anzeigen"-Schaltfläche im Suche-Protokoll, Suchfrage
    UND alle aktiven Filter exakt zu reproduzieren -- siehe index.html, liest ihn
    beim Laden aus der URL und stellt Suchfeld/Filter entsprechend wieder her.

    Die SELECT-dann-INSERT/UPDATE-Prüfung für token läuft bewusst in einer
    BEGIN-IMMEDIATE-Transaktion: zwei Anfragen desselben Tippvorgangs (z.B.
    Text tippen UND direkt danach einen Filter umschalten) können am Server
    nahezu gleichzeitig ankommen. Ohne expliziten Schreib-Lock VOR dem SELECT
    sehen beide Anfragen "noch keine Zeile für dieses token" und legen fälschlich
    ZWEI Zeilen an, statt sich zu einer zusammenzufassen -- genau das Problem, das
    token eigentlich lösen soll. WAL-Modus (siehe db/connection.py) plus 30s
    Timeout auf der Connection sorgen dafür, dass die zweite Anfrage einfach kurz
    wartet statt mit "database is locked" zu scheitern."""
    try:
        if token:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT id FROM search_log WHERE token = ? AND clicks = 0 "
                    "AND ts >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?) "
                    "ORDER BY id DESC LIMIT 1",
                    (token, f"-{_COALESCE_WINDOW_S} seconds"),
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE search_log SET ts = strftime('%Y-%m-%dT%H:%M:%SZ','now'), "
                        "kind = ?, query = ?, project_id = ?, filters = ?, "
                        "result_count = ?, duration_ms = ?, query_string = ? WHERE id = ?",
                        (kind, query or None, project_id, filters or None,
                         result_count, duration_ms, query_string or None, existing["id"]),
                    )
                    row_id = existing["id"]
                else:
                    cur = conn.execute(
                        """INSERT INTO search_log
                           (kind, query, project_id, filters, result_count, duration_ms, token, query_string)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (kind, query or None, project_id, filters or None, result_count, duration_ms,
                         token, query_string or None),
                    )
                    row_id = cur.lastrowid
                conn.commit()
                return row_id
            except Exception:
                conn.rollback()
                raise
        with conn:
            cur = conn.execute(
                """INSERT INTO search_log
                   (kind, query, project_id, filters, result_count, duration_ms, token, query_string)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (kind, query or None, project_id, filters or None, result_count, duration_ms,
                 None, query_string or None),
            )
            return cur.lastrowid
    except Exception as exc:
        log.warning("Suche-Log fehlgeschlagen (kind=%s): %s", kind, exc)
        return None


def log_click(conn: sqlite3.Connection, search_log_id: int) -> None:
    """Zählt hoch, dass aus dieser Suche ein Treffer geöffnet/im Finder gezeigt
    wurde -- absichtlich ohne festzuhalten WELCHER Treffer, das würde wieder auf
    einzelne Dokumente (und damit indirekt Personen) zurückführen, was hier
    explizit nicht das Ziel ist."""
    try:
        with conn:
            conn.execute(
                "UPDATE search_log SET clicks = clicks + 1 WHERE id = ?", (search_log_id,)
            )
    except Exception as exc:
        log.warning("Klick-Zählung fehlgeschlagen (id=%s): %s", search_log_id, exc)


def cleanup_old(conn: sqlite3.Connection, retention_days: int) -> int:
    """Löscht Protokollzeilen älter als retention_days. Gibt die Anzahl gelöschter
    Zeilen zurück (0 bei retention_days <= 0, was 'nie löschen' bedeuten würde --
    kein Aufrufer soll das versehentlich mit 'sofort alles löschen' verwechseln)."""
    if retention_days <= 0:
        return 0
    with conn:
        cur = conn.execute(
            "DELETE FROM search_log WHERE ts < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)",
            (f"-{retention_days} days",),
        )
        return cur.rowcount
