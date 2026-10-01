#!/usr/bin/env python3
"""Auswertung des Ablage-Protokolls aus dem Alltag (Nachtrag 1 §5.6).

    ARCHIVIO_DB=<pfad zur archivio.db> .venv/bin/python scripts/ablage_log_auswertung.py [--seit 2026-10-01] [--host mac-1]

Zeigt, ob die Ablage-Funktion im Alltag schneller ist als selbst suchen: Anteil der Wege (Option / zuletzt / Suche /
Browser / neuer Ordner), Median und 90. Perzentil der Zeit von Seitenaufruf bis Ablegen, Rang-Verteilung der gewählten
Option und wie oft das vorgeschlagene Projekt stimmte. Die Datenbank wird nur gelesen. Erst nach dieser Auswertung wird
über weiteres Scoring-Tuning und Regel-Vorschläge entschieden.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

WEGE = [("option", "Vorgeschlagene Option"), ("zuletzt", "Zuletzt verwendet"), ("suche", "Ordnersuche"),
        ("neuer_ordner", "Neuer datierter Ordner"), ("browser", "Ordnerbrowser")]


def pct(a, b):
    return f"{100 * a / b:.0f} %" if b else "–"


def perzentil(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else None


def sekunden(ms):
    return "–" if ms is None else f"{ms / 1000:.1f} s"


def auswerten(conn, seit: str | None = None, host: str | None = None) -> str:
    sql, args = "SELECT * FROM ablage_log WHERE 1=1", []
    if seit:
        sql += " AND ts >= ?"
        args.append(seit)
    if host:
        sql += " AND host = ?"
        args.append(host)
    rows = [dict(r) for r in conn.execute(sql, args)]
    n = len(rows)
    out = ["# Ablage-Protokoll aus dem Alltag", ""]
    if not n:
        return "\n".join(out + ["Noch keine Einträge im Protokoll."])
    out.append(f"- Ablagen: **{n}** · Zeitraum {min(r['ts'] for r in rows)[:10]} bis {max(r['ts'] for r in rows)[:10]} · "
               f"Rechner: {', '.join(sorted({r['host'] or '?' for r in rows}))}")
    mit_zeit = [r for r in rows if r["seite_ms"] is not None]
    out.append(f"- Median Zeit Seitenaufruf → Ablegen: **{sekunden(statistics.median(r['seite_ms'] for r in mit_zeit) if mit_zeit else None)}** "
               f"· 90. Perzentil {sekunden(perzentil([r['seite_ms'] for r in mit_zeit], 0.9))} (n={len(mit_zeit)})")
    ohne = sum(1 for r in rows if not r["quelle"])
    if ohne:
        out.append(f"- {ohne} ältere Einträge ohne Kennzeichen (vor Migration 035) zählen unter „Unbekannt“")
    out += ["", "## Welcher Weg wurde benutzt?", "", "| Weg | Anteil | Anzahl | Median Zeit | 90. Perzentil |", "|---|---:|---:|---:|---:|"]
    nach = defaultdict(list)
    for r in rows:
        nach[r["quelle"] or "unbekannt"].append(r)
    for k, name in WEGE + [("unbekannt", "Unbekannt")]:
        gr = nach.get(k, [])
        if not gr:
            continue
        zeiten = [r["seite_ms"] for r in gr if r["seite_ms"] is not None]
        out.append(f"| {name} | {pct(len(gr), n)} | {len(gr)} | {sekunden(statistics.median(zeiten) if zeiten else None)} | "
                   f"{sekunden(perzentil(zeiten, 0.9))} |")
    out += ["", "## Rang der gewählten Option", "", "| Rang | Anzahl | Anteil aller Ablagen |", "|---|---:|---:|"]
    rang = Counter(r["gewaehlt_rang"] for r in rows)
    for k, name in ((1, "Option 1"), (2, "Option 2"), (3, "Option 3"), (0, "keine Option (Suche, Browser, zuletzt, neuer Ordner)")):
        out.append(f"| {name} | {rang.get(k, 0)} | {pct(rang.get(k, 0), n)} |")
    treffer = sum(1 for r in rows if r["gewaehlt_rang"] in (1, 2, 3))
    out += ["", f"**Das Ziel stand unter den Vorschlägen: {pct(treffer, n)}** · davon gleich Option 1: {pct(rang.get(1, 0), n)}", ""]
    pr = [r for r in rows if r["projekt_richtig"] is not None]
    out.append(f"- Das vorgeschlagene Projekt war richtig: **{pct(sum(r['projekt_richtig'] for r in pr), len(pr))}** (n={len(pr)})")
    dauer = [r["dauer_ms"] for r in rows if r["dauer_ms"] is not None]
    out.append(f"- Median Zeit Analyse → Ablage (Server-Sicht, inkl. Wartezeit des Nutzers): {sekunden(statistics.median(dauer) if dauer else None)}")
    endungen = Counter(r["endung"] or "?" for r in rows).most_common(6)
    out.append("- Häufigste Endungen: " + ", ".join(f"{e} ({c})" for e, c in endungen))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.environ.get("ARCHIVIO_DB"))
    ap.add_argument("--seit", help="nur Einträge ab diesem Datum (ISO)")
    ap.add_argument("--host", help="nur ein Rechner")
    a = ap.parse_args()
    if not a.db or not Path(a.db).exists():
        sys.exit("Datenbank nicht gefunden: ARCHIVIO_DB oder --db angeben")
    conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    print(auswerten(conn, a.seit, a.host))


if __name__ == "__main__":
    main()
