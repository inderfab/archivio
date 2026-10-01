#!/usr/bin/env python3
"""Messung des Ablage-Vorschlags gegen eine echte Datenbank (nur lesend).

    ARCHIVIO_DB=<pfad zur archivio.db> .venv/bin/python scripts/ablage_messung.py [Optionen]

Zeit-Trennung (planung/datei-drop-ablage.md §7): getestet werden Dateien, die NEUER sind als
der Stichtag T (Standard: vor 90 Tagen). Für jede Bewertung ist alles ab T unsichtbar, ebenso
die Testdatei selbst; die Ordnerstruktur darf vollständig bekannt sein. Die Datenbank wird mit
`mode=ro` geöffnet und nie geschrieben.

Stand Etappe 4: Basiswert (die alte Logik aus feat/datei-drop, eingefroren in diesem Skript),
Merkmals-Abdeckung, Stichproben-Dateinamen und Zeit des Statistik-Aufbaus. Die neue Logik
kommt in Etappe 5 hinzu (zwei Läufe: mit und ohne Vorgänger-Signal).
"""
from __future__ import annotations

import argparse
import os
import random
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner.ablage import index as ablage_index                      # noqa: E402
from scanner.ablage import merkmale as mk                             # noqa: E402
from scanner.ablage import vorlage                                    # noqa: E402
from scanner.ablage.normalisieren import art_bestimmen, label_pfad, zerlege  # noqa: E402

# ── Eingefrorene alte Logik (feat/datei-drop, scanner/walker.py) ────────────────
# Absichtlich kopiert statt importiert: Etappe 5 entfernt die Originale aus walker.py,
# der Basiswert muss reproduzierbar bleiben.

_ALT_STOPP = {
    "der", "die", "das", "und", "oder", "mit", "für", "von", "auf", "ist",
    "ein", "eine", "einer", "eines", "einem", "einen", "den", "dem", "des",
    "im", "am", "zum", "zur", "als", "auch", "aus", "bei", "nach", "vor",
    "nicht", "sich", "sind", "war", "wird", "wurde", "haben", "hat",
    "the", "and", "for", "with", "this", "that", "from",
}
_ALT_WORT4 = re.compile(r"[^\W\d_]{4,}", re.UNICODE)
_ALT_WORT3 = re.compile(r"[^\W\d_]{3,}", re.UNICODE)
_ALT_NUMMER = re.compile(r"^(\d{2,})[\s_-]")


def _alt_tokens(stem: str) -> set[str]:
    return {w for w in _ALT_WORT3.findall(stem.lower()) if w not in _ALT_STOPP}


def alt_projekt(conn, projekte: list, datei: dict, stichtag_roh: str) -> int | None:
    """Wie suggest_project_for_file: führende Nummer, sonst FTS-ODER-Abfrage (ohne die Testdatei
    selbst und ohne Dokumente ab Stichtag, sonst findet sie sich selbst)."""
    m = _ALT_NUMMER.match(Path(datei["filename"]).stem)
    if m:
        treffer = [p for p in projekte if p["name"].startswith(m.group(1) + " ")]
        if treffer:
            return sorted(treffer, key=lambda p: (-p["active"], p["id"]))[0]["id"]
    worte, gesehen = [], set()
    for (inhalt,) in conn.execute(
            "SELECT content FROM document_chunks WHERE document_id = ? ORDER BY chunk_index", (datei["id"],)):
        for w in _ALT_WORT4.findall((inhalt or "").lower()):
            if w in _ALT_STOPP or w in gesehen:
                continue
            gesehen.add(w)
            worte.append(w)
            if len(worte) >= 40:
                break
        if len(worte) >= 40:
            break
    if not worte:
        return None
    q = " OR ".join(f'"{w}"' for w in worte)
    try:
        r = conn.execute(
            "SELECT d.project_id, COUNT(*) AS n FROM chunks_fts "
            "JOIN document_chunks dc ON chunks_fts.rowid = dc.id JOIN documents d ON d.id = dc.document_id "
            "WHERE chunks_fts MATCH ? AND d.project_id IS NOT NULL AND d.id <> ? AND d.modified_at < ? "
            "GROUP BY d.project_id ORDER BY n DESC LIMIT 1", (q, datei["id"], stichtag_roh)).fetchone()
    except sqlite3.Error:
        return None
    return r[0] if r else None


def alt_ordner(sicht: list[dict], datei: dict) -> str | None:
    """Stufe 1 von _suggest_destination_folder_raw: Ordner mit gleicher Endung im Projekt, bester
    Wortüberschneidung, Gleichstand → zuletzt geändert. Stufe 2 (os.walk) entfällt: sie fasst das
    NAS an und lässt sich zeitlich nicht trennen. Ohne Treffer: kein Vorschlag."""
    neu = _alt_tokens(Path(datei["filename"]).stem)
    punkte: dict[str, int] = {}
    zuletzt: dict[str, str] = {}
    for d in sicht:
        if d["ext"] != datei["ext"]:
            continue
        s = len(neu & d["tokens"]) if neu else 0
        f = d["ordner"]
        if f not in punkte or s > punkte[f]:
            punkte[f] = s
        if d["mod"] > zuletzt.get(f, ""):
            zuletzt[f] = d["mod"]
    if not punkte:
        return None
    best = max(punkte.values())
    kand = [f for f in punkte if best == 0 or punkte[f] == best]
    return max(kand, key=lambda f: zuletzt.get(f, ""))


# ── Daten laden ────────────────────────────────────────────────────────────────

def utc_iso(roh) -> str:
    d = mk._parse_mtime(roh)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ") if d else ""


def lade(conn, args):
    projekte = [dict(r) for r in conn.execute("SELECT id, name, path, active FROM projects")]
    ok = []
    for p in projekte:
        if p["path"].startswith("mailbox:") or vorlage.ist_musterprojekt(p["path"]):
            continue
        nr = vorlage.projekt_nummer(p["name"], p["path"])
        if nr is None and not args.auch_ohne_nummer:
            continue
        if args.ab_projektnummer and (nr is None or nr < args.ab_projektnummer):
            continue
        ok.append(p)
    by_id = {p["id"]: p for p in ok}
    dateien = []
    for p in ok:
        root = p["path"].rstrip("/")
        for r in conn.execute(
                "SELECT d.id, d.filename, d.extension, d.filesize, d.modified_at, dp.path FROM document_paths dp "
                "JOIN documents d ON d.id = dp.document_id "
                "WHERE d.project_id = ? AND dp.is_primary = 1 AND d.source_type = 'filesystem'", (p["id"],)):
            pfad = r["path"]
            if not pfad.startswith(root + "/"):
                continue
            rel = os.path.dirname(pfad[len(root) + 1:])
            dateien.append({"id": r["id"], "project_id": p["id"], "filename": r["filename"],
                            "ext": (r["extension"] or "").lower(), "filesize": r["filesize"],
                            "modified_raw": r["modified_at"], "mod": utc_iso(r["modified_at"]),
                            "ordner": rel, "tokens": _alt_tokens(Path(r["filename"]).stem)})
    return projekte, by_id, dateien


def ordner_aus_dateien(dateien) -> dict[int, set[str]]:
    """Ordner je Projekt, abgeleitet aus den Dateipfaden (ohne leere Ordner — der Rückfall,
    solange `ablage_ordner` noch nicht durch einen Scan gefüllt ist)."""
    res: dict[int, set[str]] = defaultdict(set)
    for d in dateien:
        teile = d["ordner"].split("/") if d["ordner"] else []
        for i in range(1, len(teile) + 1):
            res[d["project_id"]].add("/".join(teile[:i]))
    return res


def top_gruppe(rel: str) -> str:
    return zerlege(rel.split("/")[0]).label if rel else "(Wurzel)"


def stichprobe(testdateien, n, rnd):
    """Reihum aus allen Top-Level-Gruppen, damit kleine Bereiche nicht untergehen."""
    gruppen: dict[str, list] = defaultdict(list)
    for d in testdateien:
        gruppen[top_gruppe(d["ordner"])].append(d)
    for g in gruppen.values():
        rnd.shuffle(g)
    res = []
    while len(res) < n and any(gruppen.values()):
        for g in sorted(gruppen):
            if gruppen[g] and len(res) < n:
                res.append(gruppen[g].pop())
    return res


# ── Auswertung ─────────────────────────────────────────────────────────────────

def pct(a, b):
    return f"{100 * a / b:.1f} %" if b else "–"


def p95(xs):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * 0.95))]


def baseline(conn, projekte, test, dateien_by_proj, stichtag_roh, stichtag, ohne_fts):
    zeilen = []
    for i, f in enumerate(test, 1):
        if i % 100 == 0:
            print(f"  Basiswert {i}/{len(test)}", file=sys.stderr)
        t0 = time.perf_counter()
        vorschlag_p = None if ohne_fts and not _ALT_NUMMER.match(Path(f["filename"]).stem) else \
            alt_projekt(conn, projekte, f, stichtag_roh)
        dt_p = (time.perf_counter() - t0) * 1000
        sicht = [d for d in dateien_by_proj[f["project_id"]] if d["mod"] < stichtag and d["id"] != f["id"]]
        t0 = time.perf_counter()
        vorschlag_o = alt_ordner(sicht, f)
        dt_o = (time.perf_counter() - t0) * 1000
        zeilen.append({"gruppe": top_gruppe(f["ordner"]), "projekt_ok": vorschlag_p == f["project_id"],
                       "projekt_da": vorschlag_p is not None, "ordner_ok": vorschlag_o == f["ordner"],
                       "ordner_da": vorschlag_o is not None, "ms": dt_p + dt_o})
    return zeilen


def tabelle(zeilen):
    gruppen: dict[str, list] = defaultdict(list)
    for z in zeilen:
        gruppen[z["gruppe"]].append(z)
    out = ["| Bereich | n | Projekt Top-1 | Projekt ohne Vorschlag | Ordner Top-1 (Projekt bekannt) | Ordner ohne Vorschlag |",
           "|---|---:|---:|---:|---:|---:|"]

    def zeile(name, zs):
        n = len(zs)
        return (f"| {name} | {n} | {pct(sum(z['projekt_ok'] for z in zs), n)} | "
                f"{pct(sum(not z['projekt_da'] for z in zs), n)} | {pct(sum(z['ordner_ok'] for z in zs), n)} | {pct(sum(not z['ordner_da'] for z in zs), n)} |")
    out.append(zeile("**gesamt**", zeilen))
    for g in sorted(gruppen, key=lambda g: -len(gruppen[g])):
        out.append(zeile(g, gruppen[g]))
    return "\n".join(out)


def merkmals_abdeckung(test, wb, bekannte):
    zaehler: Counter = Counter()
    tokens: Counter = Counter()
    for f in test:
        m = mk.merkmale(f["filename"], f["filesize"], f["modified_raw"], None, None, wb, bekannte)
        for t in {k.split(":")[0] for k in m if not k.startswith(("ext:", "tok:"))}:
            zaehler[t] += 1
        for k in m:
            if k.startswith("tok:"):
                tokens[k[4:]] += 1
    n = len(test)
    zeilen = ["| Merkmal | Dateien mit Treffer |", "|---|---:|"]
    for t in ("proj", "phase", "plantyp", "geschoss", "massstab", "doktyp", "bkp", "fp", "index", "datum"):
        zeilen.append(f"| {t} | {pct(zaehler[t], n)} ({zaehler[t]}) |")
    return "\n".join(zeilen), tokens.most_common(25)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.environ.get("ARCHIVIO_DB"), help="Pfad zur archivio.db (oder ARCHIVIO_DB)")
    ap.add_argument("--tage", type=int, default=90, help="Stichtag T = heute minus N Tage (90)")
    ap.add_argument("--stichtag", help="T explizit (ISO, z. B. 2026-07-03) statt --tage; macht Läufe reproduzierbar")
    ap.add_argument("--stichprobe", type=int, default=2000)
    ap.add_argument("--stichproben-namen", type=int, default=300, help="Anzahl Dateinamen zur Sichtprüfung")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--ab-projektnummer", type=int, default=0, help="nur Projekte ab dieser Nummer (Strut: 184)")
    ap.add_argument("--auch-ohne-nummer", action="store_true", help="auch Ordner ohne führende Projektnummer")
    ap.add_argument("--ohne-fts", action="store_true", help="Basiswert ohne Volltext-Signal (schneller)")
    ap.add_argument("--ausgabe", default=None, help="Berichtsdatei (Standard: ablage_messung_<datum>.md)")
    args = ap.parse_args()
    if not args.db or not Path(args.db).exists():
        sys.exit("Datenbank nicht gefunden: ARCHIVIO_DB oder --db angeben")

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rnd = random.Random(args.seed)
    jetzt = datetime.now(timezone.utc)
    T = (datetime.fromisoformat(args.stichtag).replace(tzinfo=timezone.utc) if args.stichtag
         else jetzt - timedelta(days=args.tage))
    stichtag = T.strftime("%Y-%m-%dT%H:%M:%SZ")

    projekte, by_id, dateien = lade(conn, args)
    if not dateien:
        sys.exit("Keine Dateien in nummerierten Projekten gefunden (--auch-ohne-nummer?)")
    by_proj: dict[int, list] = defaultdict(list)
    for d in dateien:
        by_proj[d["project_id"]].append(d)
    test_alle = [d for d in dateien if d["mod"] >= stichtag]
    test = stichprobe(test_alle, args.stichprobe, rnd)

    out: list[str] = []
    w = out.append
    w(f"# Ablage-Messung {jetzt:%Y-%m-%d}\n")
    w(f"- Datenbank: `{args.db}` (nur lesend)")
    w(f"- Stichtag T: {stichtag} · Projekte: {len(by_id)} · Dateien: {len(dateien)} · "
      f"davon ab T: {len(test_alle)} · Stichprobe: {len(test)} (Seed {args.seed})")
    if args.ab_projektnummer:
        w(f"- Nur Projekte ab Nummer {args.ab_projektnummer}")
    w("- Ordnerstruktur aus den Dateipfaden abgeleitet (leere Ordner fehlen, solange `ablage_ordner` nicht gefüllt ist)\n")
    if not test:
        w("**Keine Testdateien ab dem Stichtag.** Ein früherer Stichtag (`--tage`/`--stichtag`) hilft.\n")

    # Basiswert
    w("## Basiswert: alte Logik (feat/datei-drop)\n")
    w("Projekt = führende Nummer, sonst Volltext-ODER-Abfrage (Testdatei und Dokumente ab T ausgeblendet). "
      "Ordner = Stufe 1 der alten Logik (gleiche Endung im Projekt, Wortüberschneidung, sonst zuletzt "
      "geändert), **gegeben das richtige Projekt**. Stufe 2 (`os.walk`) entfällt, siehe Skript. "
      "Die alte Logik liefert nur einen Vorschlag: Top-3 und „sicher-falsch\" gibt es dort nicht.\n")
    if test:
        zeilen = baseline(conn, projekte, test, by_proj, stichtag, stichtag, args.ohne_fts)
        w(tabelle(zeilen))
        ms = [z["ms"] for z in zeilen]
        w(f"\nLaufzeit je Vorschlag: Median {statistics.median(ms):.1f} ms · p95 {p95(ms):.1f} ms"
          + (" (ohne Volltext-Signal)" if args.ohne_fts else ""))
        w(f"\nOrdner-Treffer bei vorhandenem Vorschlag: "
          f"{pct(sum(z['ordner_ok'] for z in zeilen), sum(z['ordner_da'] for z in zeilen))}\n")

    # Struktur / Statistik
    w("## Struktur und Statistik-Neuaufbau\n")
    ordner_proj = ordner_aus_dateien(dateien)
    lern = {p: {label_pfad(o) for o in os_ if label_pfad(o)} for p, os_ in ordner_proj.items()
            if len(os_) >= vorlage.MIN_ORDNER}
    slots, genutzt = vorlage.herleiten(lern) if lern else ({}, [])
    w(f"- Lern-Projekte (≥ {vorlage.MIN_ORDNER} Ordner): {len(lern)}, davon nach Aussortieren genutzt: {len(genutzt)}")
    w(f"- Hergeleitete Slots (Abdeckung ≥ {vorlage.ABDECKUNG_MIN:.0%}): {len(slots)}")
    slot_id = {lp: i for i, lp in enumerate(sorted(slots), 1)}
    ordner, ordner_id, id_ = {}, {}, 0
    for p, os_ in ordner_proj.items():
        for o in sorted(os_, key=lambda x: x.count("/")):
            id_ += 1
            ordner_id[(p, o)] = id_
            name = o.rsplit("/", 1)[-1]
            z = zerlege(name)
            ordner[id_] = {"slot_id": slot_id.get(label_pfad(o)), "rolle": z.label,
                           "art": art_bestimmen(name, z.label),
                           "parent_id": ordner_id.get((p, o.rsplit("/", 1)[0])) if "/" in o else None}
    lern_ids = set(genutzt)
    stat_dateien = ({**d, "ordner_id": ordner_id[(d["project_id"], d["ordner"])], "modified_at": d["modified_raw"]}
                    for d in dateien if d["project_id"] in lern_ids and d["ordner"])
    bkp_zeilen = []
    for pid, os_ in ordner_proj.items():
        pnr = vorlage.projekt_nummer(by_id[pid]["name"], by_id[pid]["path"]) if pid in by_id else None
        for name in {o.rsplit("/", 1)[-1] for o in os_}:
            z = zerlege(name)
            bkp_zeilen.append((z.label, z.codes, pnr))
    bkp = mk.bkp_woerter_aus_ordnern(bkp_zeilen, mk.woerterbuch().ist_allgemein)
    wb = mk.woerterbuch().mit_bkp_woertern(bkp)
    t0 = time.perf_counter()
    stat = ablage_index.statistik_berechnen(stat_dateien, ordner, wb, jetzt, nur_vor=stichtag)
    dt = time.perf_counter() - t0
    zeilen_n = sum(len(z) for z in stat.values())
    w(f"- Statistik (nur Dateien vor T): {zeilen_n} Zeilen in {dt:.1f} s (Ziel auf dem iMac: < 60 s mit allen Dateien)")
    w(f"- Gelernte BKP-Bezeichnungen: {len(bkp)}\n")

    # Merkmale
    w("## Merkmals-Abdeckung (Stichprobe)\n")
    bekannte = {str(vorlage.projekt_nummer(p['name'], p['path'])) for p in projekte
                if vorlage.projekt_nummer(p['name'], p['path']) is not None}
    if test:
        tab, top_tokens = merkmals_abdeckung(test, wb, bekannte)
        w(tab)
        w("\nHäufigste Dateinamen-Tokens: " + ", ".join(f"{t} ({n})" for t, n in top_tokens) + "\n")

    # Stichproben-Dateinamen
    w(f"## Stichproben-Dateinamen ({args.stichproben_namen}, nach Top-Ordner)\n")
    w("Zur Sichtprüfung der Merkmals-Erkennung. Pfade relativ zum Projekt.\n")
    pool = [d for d in dateien if d["project_id"] in by_id]
    namen = rnd.sample(pool, min(args.stichproben_namen, len(pool)))
    gr: dict[str, list] = defaultdict(list)
    for d in namen:
        gr[d["ordner"].split("/")[0] if d["ordner"] else "(Wurzel)"].append(d)
    for g in sorted(gr):
        w(f"**{g}**")
        for d in sorted(gr[g], key=lambda x: x["filename"]):
            k = sorted(x for x in mk.merkmale(d["filename"], wb=wb, bekannte_projekte=bekannte)
                       if not x.startswith(("tok:", "ext:")))
            w(f"- `{d['filename']}` — {', '.join(k) or '–'}")
        w("")

    w("## Neue Logik\n")
    w("Folgt in Etappe 5 (Vorstufen, Scoring, Hierarchie-Konfidenz; zwei Läufe mit/ohne Vorgänger-Signal).\n")

    bericht = "\n".join(out)
    print(bericht)
    ziel = Path(args.ausgabe or f"ablage_messung_{jetzt:%Y-%m-%d}.md")
    ziel.write_text(bericht, encoding="utf-8")
    print(f"\nBericht geschrieben: {ziel}", file=sys.stderr)


if __name__ == "__main__":
    main()
