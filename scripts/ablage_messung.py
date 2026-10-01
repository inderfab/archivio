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
from scanner.ablage import ordner as od                               # noqa: E402
from scanner.ablage import projekt as pj                              # noqa: E402
from scanner.ablage import vorlage                                    # noqa: E402
from scanner.ablage.kontext import Kontext, OrdnerInfo, Parameter, Projekt, Slot  # noqa: E402
from scanner.ablage.vorschlag import ordner_vorschlag                 # noqa: E402
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


def baseline(conn, projekte, test, dateien_by_proj, stichtag_roh, stichtag, ohne_fts, cut_fn=None):
    zeilen = []
    for i, f in enumerate(test, 1):
        if i % 100 == 0:
            print(f"  Basiswert {i}/{len(test)}", file=sys.stderr)
        t0 = time.perf_counter()
        cut = cut_fn(f) if cut_fn else stichtag
        vorschlag_p = None if ohne_fts and not _ALT_NUMMER.match(Path(f["filename"]).stem) else \
            alt_projekt(conn, projekte, f, cut)
        dt_p = (time.perf_counter() - t0) * 1000
        sicht = [d for d in dateien_by_proj[f["project_id"]] if d["mod"] < cut and d["id"] != f["id"]]
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



# ── Neue Logik ─────────────────────────────────────────────────────────────────

def lies_muster(pfad: str | None) -> list[str]:
    """Relative Ordnerpfade der Vorlage: Ordner (NAS-Pfad) oder Textdatei mit einem Pfad je Zeile."""
    if not pfad:
        return []
    p = Path(pfad)
    if p.is_dir():
        return vorlage._lies_baum(str(p))
    if p.is_file():
        return [z for z in p.read_text(encoding="utf-8").splitlines() if z.strip()]
    sys.exit(f"Musterordner nicht gefunden: {pfad}")


def baue_kontext(conn, projekte, by_id, dateien, stichtag, muster, jetzt, ohne_fts):
    """Der Kontext, wie die Engine ihn zum Stichtag gekannt hätte: Struktur komplett (sie existierte),
    Statistik, Vorgänger und Aktivität nur aus Dateien VOR dem Stichtag."""
    from collections import Counter as C
    ctx = Kontext(jetzt=jetzt)
    for p in by_id.values():
        vor = [d["mod"] for d in dateien if d["project_id"] == p["id"] and d["mod"] < stichtag]
        ctx.projekte[p["id"]] = Projekt(p["id"], p["name"], p["path"], vorlage.projekt_nummer(p["name"], p["path"]),
                                        bool(p["active"]), max(vor) if vor else None)
    # Slots: aus dem Musterordner (Weg A), sonst aus den Projekten hergeleitet (Weg B)
    namen: dict[str, C] = defaultdict(C)
    if muster:
        for rel in muster:
            if any(zerlege(t).label == "" for t in rel.split("/")):
                continue
            lp = label_pfad(rel)
            if lp:
                namen[lp][rel.rsplit("/", 1)[-1]] += 1
        slot_lps = {lp: 0.0 for lp in namen}
        aus_vorlage = True
    else:
        os_ = ordner_aus_dateien(dateien)
        pro = {p: {label_pfad(o) for o in s_ if label_pfad(o)} for p, s_ in os_.items() if len(s_) >= vorlage.MIN_ORDNER}
        slot_lps, _ = vorlage.herleiten(pro)
        aus_vorlage = False
        for p, s_ in os_.items():
            for o in s_:
                namen[label_pfad(o)][o.rsplit("/", 1)[-1]] += 1
    slot_id = {}
    for i, lp in enumerate(sorted(slot_lps), 1):
        slot_id[lp] = i
        ctx.slots[i] = Slot(i, lp, lp.rsplit("/", 1)[-1], namen[lp].most_common(1)[0][0], aus_vorlage, slot_lps[lp])
    # Ordner je Projekt
    ordner_proj = ordner_aus_dateien(dateien)
    id_ = 0
    oid: dict[tuple, int] = {}
    vor_dateien = [d for d in dateien if d["mod"] < stichtag and d["ordner"]]
    anzahl: Counter = Counter()
    neueste: dict[tuple, str] = {}
    for d in vor_dateien:
        k = (d["project_id"], d["ordner"])
        anzahl[k] += 1
        if d["mod"] > neueste.get(k, ""):
            neueste[k] = d["mod"]
    # Struktur ZUM STICHTAG: ein Ordner existierte, wenn in seinem Teilbaum vor T eine Datei lag. Mit der
    # heutigen vollständigen Struktur wären 93 % der Testdateien „in einen Ordner, den es damals noch
    # nicht gab" — das passiert beim echten Drop nie (siehe Bericht).
    existierte: set[tuple] = set()
    for d in vor_dateien:
        teile = d["ordner"].split("/")
        for i in range(1, len(teile) + 1):
            existierte.add((d["project_id"], "/".join(teile[:i])))
    for pid, rels in ordner_proj.items():
        root = by_id[pid]["path"].rstrip("/")
        for rel in sorted(rels, key=lambda x: (x.count("/"), x)):
            if (pid, rel) not in existierte:
                continue
            id_ += 1
            oid[(pid, rel)] = id_
            name = rel.rsplit("/", 1)[-1]
            z = zerlege(name)
            art = art_bestimmen(name, z.label)
            lp = label_pfad(rel)
            ctx.ordner[id_] = OrdnerInfo(id_, pid, f"{root}/{rel}", rel, name, z.label,
                                         oid.get((pid, rel.rsplit("/", 1)[0])) if "/" in rel else None, art,
                                         slot_id.get(lp) if art in ("normal", "archiv") else None,
                                         anzahl[(pid, rel)], neueste.get((pid, rel)))
    # Statistik (nur vor dem Stichtag, Alter relativ zum Stichtag) und gelernte BKP-Wörter
    bkp_zeilen = []
    for pid, rels in ordner_proj.items():
        pnr = ctx.projekte[pid].nummer
        for name in {r.rsplit("/", 1)[-1] for r in rels}:
            z = zerlege(name)
            bkp_zeilen.append((z.label, z.codes, pnr))
    wb = mk.woerterbuch().mit_bkp_woertern(mk.bkp_woerter_aus_ordnern(bkp_zeilen, mk.woerterbuch().ist_allgemein))
    info = {i: {"slot_id": o.slot_id, "rolle": o.label, "art": o.art, "parent_id": o.parent_id}
            for i, o in ctx.ordner.items()}
    stat_dateien = ({**d, "ordner_id": oid.get((d["project_id"], d["ordner"])), "modified_at": d["modified_raw"]}
                    for d in dateien if d["ordner"])
    ctx.stats = ablage_index.statistik_berechnen(stat_dateien, info, wb, jetzt, nur_vor=stichtag)
    # Hooks: Vorgänger aus dem Speicher (strikt, nur Dateien vor T), Volltext aus der DB (ohne Dokumente ab T)
    vg: dict[str, list] = defaultdict(list)
    for d in vor_dateien:
        o = ctx.ordner[oid[(d["project_id"], d["ordner"])]]
        vg[mk.vorgaenger_name(d["filename"])].append((d["project_id"], o.id, f"{o.path}/{d['filename']}", d["filename"]))
    ctx.vorgaenger_fn = lambda vname, pid: [t for t in vg.get(vname, []) if pid is None or t[0] == pid]
    if not ohne_fts:
        def fts(woerter):
            q = " OR ".join(f'"{w}"' for w in woerter)
            try:
                rows = conn.execute(
                    "SELECT d.project_id, COUNT(*) AS n FROM chunks_fts JOIN document_chunks dc ON chunks_fts.rowid = dc.id "
                    "JOIN documents d ON d.id = dc.document_id WHERE chunks_fts MATCH ? AND d.project_id IS NOT NULL "
                    "AND d.modified_at < ? GROUP BY d.project_id ORDER BY n DESC LIMIT 3", (q, stichtag)).fetchall()
            except sqlite3.Error:
                return {}
            g = sum(r[1] for r in rows) or 1
            return {r[0]: r[1] / g for r in rows}
        ctx.fts_fn = fts
    ctx.fertig()
    return ctx, wb, oid


def chunk_text(conn, doc_id, n=mk.INHALT_ZEICHEN):
    text = ""
    for (c,) in conn.execute("SELECT content FROM document_chunks WHERE document_id = ? ORDER BY chunk_index", (doc_id,)):
        text += (c or "") + "\n"
        if len(text) >= n:
            break
    return text[:n] or None


def bewerte_neu(conn, ctx, wb, test, par_basis, ohne_fts, oid):
    """Pro Testdatei: Projekt-Ranking, Ordner-Scores (nur Statistik) und Vorschläge mit/ohne Vorgänger."""
    nummern = {str(n) for n in ctx._nummern}
    res = []
    for i, f in enumerate(test, 1):
        if i % 200 == 0:
            print(f"  Neue Logik {i}/{len(test)}", file=sys.stderr)
        text = chunk_text(conn, f["id"])
        datei = pj.DateiInfo(f["filename"], f["filesize"], f["modified_raw"], None, text)
        datei.merkmale = mk.merkmale(f["filename"], f["filesize"], f["modified_raw"], text, None, wb, nummern)
        t0 = time.perf_counter()
        pk = pj.projekt_bestimmen(ctx, datei)
        dt_p = (time.perf_counter() - t0) * 1000
        pid = f["project_id"]
        rel, ziel_neu = f["ordner"], False
        while rel and (pid, rel) not in oid:         # Ziel gab es zum Stichtag noch nicht → Elternordner
            rel, ziel_neu = (rel.rsplit("/", 1)[0] if "/" in rel else ""), True
        wahr = f"{by_pfad(ctx, pid)}/{rel}" if rel else None
        kands, scores = od.bewerten(ctx, pid, datei.merkmale, par_basis)
        res.append({"f": f, "datei": datei, "pk": pk, "dt_p": dt_p, "wahr": wahr, "kands": kands, "scores": scores,
                    "ziel_neu": ziel_neu, "gruppe": top_gruppe(f["ordner"])})
    return res


def by_pfad(ctx, pid):
    return ctx.projekte[pid].path.rstrip("/")


def auswerten_ordner(ctx, zeilen, par, mit_vorgaenger):
    """Kennzahlen für einen Parametersatz. Zieht Ordner im Archiv und im Wurzelordner aus der Wertung."""
    out = []
    for z in zeilen:
        ctx = z.get("ctx", ctx)
        if z["wahr"] is None:
            continue
        pid = z["f"]["project_id"]
        wahr_ordner = next((k for k in z["kands"] if k.pfad == z["wahr"]), None)
        if wahr_ordner is None:
            out.append({"gruppe": z["gruppe"], "ausser_wertung": True, "ziel_neu": z["ziel_neu"]})
            continue
        t0 = time.perf_counter()
        if mit_vorgaenger:
            erg = ordner_vorschlag(ctx, z["datei"], pid, par, True)
            rang = [o["pfad"] for o in erg["optionen"]]
        else:
            # Optionen = Zweige der Hierarchie (Statistik allein)
            probs = od.wahrscheinlichkeiten(z["scores"], par.temperatur)
            h = od.hierarchie(ctx, pid, z["kands"], probs, par)
            rang = [k.pfad for k, _ in h["optionen"]]
            erg = {"fall": h["fall"], "sicher_bis": {"pfad": h["sicher_pfad"], "p": h["sicher_p"]},
                   "optionen": [{"pfad": k.pfad, "p": p} for k, p in h["optionen"]]}
        ms = (time.perf_counter() - t0) * 1000
        sb = erg["sicher_bis"]["pfad"] if erg.get("sicher_bis") else None
        wurzel = ctx.projekte[pid].path.rstrip("/")
        tiefe = len([t for t in (sb or "")[len(wurzel):].split("/") if t]) if sb else 0
        sicher_ok = bool(sb) and (z["wahr"] == sb or z["wahr"].startswith(sb.rstrip("/") + "/"))
        im_zweig = [bool(r) and (z["wahr"] == r or z["wahr"].startswith(r.rstrip("/") + "/")) for r in rang]
        tiefe_o1 = len([t for t in rang[0][len(wurzel):].split("/") if t]) if rang else 0
        out.append({"gruppe": z["gruppe"], "ausser_wertung": False, "ziel_neu": z["ziel_neu"], "fall": erg["fall"],
                    "top1": bool(rang) and rang[0] == z["wahr"], "top3": z["wahr"] in rang[:3],
                    "sicher_ok": sicher_ok, "tiefe": tiefe if sicher_ok else 0,
                    "zweig1": bool(im_zweig) and im_zweig[0], "zweig3": any(im_zweig[:3]), "tiefe_o1": tiefe_o1,
                    "nutzen": (tiefe if sicher_ok else -NUTZEN_FALSCH)
                              + 0.5 * (tiefe_o1 if im_zweig and im_zweig[0] else 0),
                    "ms": ms + z["dt_p"] * 0, "vorschlag": rang[0] if rang else None, "wahr": z["wahr"],
                    "slot_wahr": wahr_ordner.label_pfad, "slot_vor": next(
                        (k.label_pfad for k in z["kands"] if rang and k.pfad == rang[0]), "")})
    return out


NUTZEN_FALSCH = 3.0     # ein falsches „sicher bis hier" kostet so viele Ebenen Nutzen wie ein richtiges Ebenen bringt


def kennzahlen(zs):
    zs = [z for z in zs if not z["ausser_wertung"]]
    n = len(zs)
    eind = [z for z in zs if z["fall"] == "eindeutig"]
    return {"n": n, "top1": sum(z["top1"] for z in zs), "top3": sum(z["top3"] for z in zs),
            "eindeutig": len(eind), "sicher_falsch": sum(not z["top1"] for z in eind),
            "sicher_ok": sum(z["sicher_ok"] for z in zs),
            "zweig1": sum(z["zweig1"] for z in zs), "zweig3": sum(z["zweig3"] for z in zs),
            "tiefe_o1": [z["tiefe_o1"] for z in zs if z["zweig1"]],
            "tiefe": [z["tiefe"] for z in zs], "nutzen": sum(z["nutzen"] for z in zs) / n if n else 0.0,
            "faelle": Counter(z["fall"] for z in zs), "ms": [z["ms"] for z in zs]}


def tabelle_neu(zs):
    k = kennzahlen(zs)
    n, e = k["n"], k["eindeutig"]
    return (f"| Ordner Top-1 (Option 1 ist genau das Ziel) | {pct(k['top1'], n)} |\n|---|---:|\n"
            f"| Ordner Top-3 (eine der Optionen ist genau das Ziel) | {pct(k['top3'], n)} |\n"
            f"| Ziel liegt im Zweig von Option 1 | {pct(k['zweig1'], n)} (mittlere Tiefe der Option {statistics.mean(k['tiefe_o1']) if k['tiefe_o1'] else 0:.1f}) |\n"
            f"| Ziel liegt im Zweig einer der 3 Optionen | {pct(k['zweig3'], n)} |\n"
            f"| Anteil „eindeutig\" | {pct(e, n)} ({e}) |\n| davon falsch (sicher-falsch) | {pct(k['sicher_falsch'], e)} ({k['sicher_falsch']}) |\n"
            f"| `sicher_bis` korrekt | {pct(k['sicher_ok'], n)} |\n"
            f"| … davon mindestens Ebene 1 / 2 / 3 tief | {pct(sum(t >= 1 for t in k['tiefe']), n)} / {pct(sum(t >= 2 for t in k['tiefe']), n)} / {pct(sum(t >= 3 for t in k['tiefe']), n)} |\n"
            f"| Nutzen (Tiefe von `sicher_bis` bei richtig, −{NUTZEN_FALSCH:g} bei falsch, plus ½ Tiefe von Option 1 im Zweig), Mittel | {k['nutzen']:.2f} |\n"
            f"| Fälle | {', '.join(f'{a} {b}' for a, b in k['faelle'].most_common())} |\n"
            f"| Laufzeit Ordner-Teil | Median {statistics.median(k['ms']) if k['ms'] else 0:.1f} ms · p95 {p95(k['ms']):.1f} ms |")


def tabelle_bereiche(zs):
    gr = defaultdict(list)
    for z in zs:
        gr[z["gruppe"]].append(z)
    zeilen = ["| Bereich | n | Top-1 | Top-3 | eindeutig | sicher-falsch | sicher_bis ok |", "|---|---:|---:|---:|---:|---:|---:|"]
    for g in sorted(gr, key=lambda g: -len(gr[g])):
        k = kennzahlen(gr[g])
        if k["n"]:
            zeilen.append(f"| {g} | {k['n']} | {pct(k['top1'], k['n'])} | {pct(k['top3'], k['n'])} | "
                          f"{pct(k['eindeutig'], k['n'])} | {pct(k['sicher_falsch'], k['eindeutig'])} | {pct(k['sicher_ok'], k['n'])} |")
    return "\n".join(zeilen)


def fehlertypen(zs, n=20):
    c = Counter((z["slot_wahr"] or "(kein Slot)", z["slot_vor"] or "(kein Slot)") for z in zs
                if not z["ausser_wertung"] and not z["top1"])
    return ["| wahrer Slot → vorgeschlagener Slot | Anzahl |", "|---|---:|"] + [
        f"| {a} → {b} | {m} |" for (a, b), m in c.most_common(n)]


def neu_bewerten(ctx, zeilen, par):
    """Scores der Statistik für einen Parametersatz neu berechnen (alpha, namens_gewicht … ändern sie)."""
    for z in zeilen:
        z["kands"], z["scores"] = od.bewerten(z.get("ctx", ctx), z["f"]["project_id"], z["datei"].merkmale, par)


def kalibrieren(ctx, train, par, grid):
    """Temperatur-Raster auf der Trainingshälfte: höchster mittlerer Nutzen (tiefes, richtiges `sicher_bis`
    zählt, ein falsches kostet). Das Ranking selbst hängt nicht von der Temperatur ab."""
    beste, bester = None, -1e9
    for t in grid:
        par.temperatur = t
        k = kennzahlen(auswerten_ordner(ctx, train, par, False))
        if k["n"] and k["nutzen"] > bester:
            beste, bester = t, k["nutzen"]
    par.temperatur = beste if beste is not None else par.temperatur
    return par.temperatur


def cut_fuer(f, T, tage):
    """Stand der Statistik für eine Testdatei: Beginn ihres N-Tage-Fensters (mindestens der Stichtag T)."""
    if tage <= 0:
        return T.strftime("%Y-%m-%dT%H:%M:%SZ")
    d = mk._parse_mtime(f["mod"])
    n = int((d - T).total_seconds() // (tage * 86400))
    return (T + timedelta(days=n * tage)).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    ap.add_argument("--musterordner", default=str(Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "musterordner_strut.txt"),
                    help="Vorlage: Ordner oder Textdatei mit Pfaden (Standard: Fixture). 'keiner' = aus Projekten herleiten")
    ap.add_argument("--rollend-tage", type=int, default=14,
                    help="Statistik-Stand der Testdateien alle N Tage erneuern (Produktion rechnet nach jedem Scan neu); 0 = fester Stichtag")
    ap.add_argument("--stichprobe-art", choices=["gleich", "natuerlich"], default="gleich",
                    help="gleich = reihum je Top-Ordner (Auftrag); natuerlich = zufällig, wie die Dateien tatsächlich anfallen")
    ap.add_argument("--delta", type=float, default=None, help="Gewicht Elternordner-Teilbaum")
    ap.add_argument("--namens-gewicht", type=float, default=None)
    ap.add_argument("--alpha", type=float, default=None)
    ap.add_argument("--idf-max", type=float, default=None)
    ap.add_argument("--prior-k", type=float, default=None)
    ap.add_argument("--ohne-neu", action="store_true", help="nur Basiswert und Merkmale, keine neue Logik")
    ap.add_argument("--temperaturen", default="0.5,1,1.5,2,3,4,6,8,12", help="Raster der Softmax-Temperatur")
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
    test = (stichprobe(test_alle, args.stichprobe, rnd) if args.stichprobe_art == "gleich"
            else rnd.sample(test_alle, min(args.stichprobe, len(test_alle))))

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
        zeilen = baseline(conn, projekte, test, by_proj, stichtag, stichtag, args.ohne_fts,
                          lambda f: cut_fuer(f, T, args.rollend_tage))
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
    if args.ohne_neu or not test:
        w("Übersprungen (`--ohne-neu` oder keine Testdateien).\n")
    else:
        muster = [] if args.musterordner == "keiner" else lies_muster(args.musterordner)
        par = Parameter()
        for feld, wert in (('delta', args.delta), ('namens_gewicht', args.namens_gewicht), ('alpha', args.alpha),
                            ('idf_max', args.idf_max), ('prior_k', args.prior_k)):
            if wert is not None:
                setattr(par, feld, wert)
        gruppen: dict[str, list] = defaultdict(list)
        for f in test:
            gruppen[cut_fuer(f, T, args.rollend_tage)].append(f)
        zeilen_neu = []
        t0 = time.perf_counter()
        for cut in sorted(gruppen):
            cut_dt = mk._parse_mtime(cut)
            ctx_g, wb_g, oid_g = baue_kontext(conn, projekte, by_id, dateien, cut, muster, cut_dt, args.ohne_fts)
            rows = bewerte_neu(conn, ctx_g, wb_g, gruppen[cut], par, args.ohne_fts, oid_g)
            for z in rows:
                z["ctx"] = ctx_g
            zeilen_neu += rows
            print(f"  Stand {cut[:10]}: {len(rows)} Dateien, {len(ctx_g.ordner)} Ordner", file=sys.stderr)
        ctx = zeilen_neu[0]["ctx"]
        w(f"- Vorlage: {'Musterordner (' + str(len(muster)) + ' Ordner)' if muster else 'aus den Projekten hergeleitet'} → "
          f"{len(ctx.slots)} Slots; {len(gruppen)} Statistik-Stände "
          f"({'alle ' + str(args.rollend_tage) + ' Tage erneuert' if args.rollend_tage else 'fester Stichtag'}), "
          f"Aufbau und Bewertung {time.perf_counter() - t0:.0f} s")
        # Zeit-Trennung beim Tuning: die frühere Hälfte der Testdateien kalibriert, die spätere wird berichtet
        zeilen_neu.sort(key=lambda z: z["f"]["mod"])
        half = len(zeilen_neu) // 2
        train, held = zeilen_neu[:half], zeilen_neu[half:]
        grid = [float(x) for x in args.temperaturen.split(",")]
        t_best = kalibrieren(ctx, train, par, grid)
        w(f"- Kalibrierung (Trainingshälfte, {len(train)} Dateien, früher als {held[0]['f']['mod'][:10] if held else '–'}): "
          f"Temperatur **{t_best}** aus {grid}; berichtet wird die spätere Hälfte ({len(held)} Dateien)\n")

        # Projekt
        def proj_kz(zs):
            n = len(zs)
            top1 = sum(1 for z in zs if z["pk"] and z["pk"][0].id == z["f"]["project_id"])
            top3 = sum(1 for z in zs if any(c.id == z["f"]["project_id"] for c in z["pk"][:3]))
            sicher = [z for z in zs if z["pk"] and z["pk"][0].p >= par.projekt_sicher]
            falsch = sum(1 for z in sicher if z["pk"][0].id != z["f"]["project_id"])
            return n, top1, top3, len(sicher), falsch
        n, t1, t3, sich, fal = proj_kz(held)
        w("### Projekt\n")
        w(f"| Projekt Top-1 | {pct(t1, n)} |\n|---|---:|\n| Projekt Top-3 | {pct(t3, n)} |\n"
          f"| Projekt „sicher\" (p ≥ {par.projekt_sicher}) | {pct(sich, n)} |\n| davon falsch | {pct(fal, sich)} ({fal}) |\n"
          f"| Laufzeit Projekt | Median {statistics.median(z['dt_p'] for z in held):.1f} ms · p95 {p95([z['dt_p'] for z in held]):.1f} ms |\n")
        # Ordner
        for titel, mv in (("ohne Vorgänger-Signal (Statistik allein)", False), ("mit Vorgänger-Signal (Bonus laut Parameter, Standard 0: nur Rückfrage-Hinweis)", True)):
            zs = auswerten_ordner(ctx, held, par, mv)
            w(f"### Ordner {titel} — bei bekanntem Projekt\n")
            ausser = sum(z["ausser_wertung"] for z in zs)
            w(f"(aus der Wertung: {ausser} Dateien, die im Archiv oder im Projektwurzelordner liegen)\n")
            w(tabelle_neu(zs) + "\n")
            for titel2, flag in (("Ziel existierte schon (Ordner mit Vorgeschichte)", False),
                                 ("Ziel war neu — Elternordner zählt als Ziel (z. B. neue datierte Sitzung)", True)):
                sub = [z for z in zs if not z["ausser_wertung"] and z["ziel_neu"] == flag]
                if sub:
                    k2 = kennzahlen(sub)
                    w(f"- *{titel2}*: n={k2['n']} · Top-1 {pct(k2['top1'], k2['n'])} · Top-3 {pct(k2['top3'], k2['n'])} · "
                      f"eindeutig {pct(k2['eindeutig'], k2['n'])} (sicher-falsch {pct(k2['sicher_falsch'], k2['eindeutig'])}) · "
                      f"`sicher_bis` ok {pct(k2['sicher_ok'], k2['n'])}")
            w("")
            if not mv:
                w("**Pro Bereich**\n")
                w(tabelle_bereiche([z for z in zs if not z["ausser_wertung"]]) + "\n")
                w("**Häufigste Fehlertypen (Top-1 falsch)**\n")
                w("\n".join(fehlertypen(zs)) + "\n")
        # Akzeptanz gegen die Ziele des Auftrags (§7), spätere Hälfte, Statistik allein
        kz = kennzahlen(auswerten_ordner(ctx, held, par, False))
        nn, ee = kz["n"], kz["eindeutig"]
        ms_o = kz["ms"]
        ms_p = [z["dt_p"] for z in held]
        def zeile(name, ziel, wert, ok):
            return f"| {name} | {ziel} | {wert} | {'✓' if ok else '✗'} |"
        sf = kz["sicher_falsch"] / ee if ee else 0.0
        w("### Akzeptanz (Ziele aus dem Auftrag, Statistik allein, spätere Hälfte)\n")
        w("| Kennzahl | Ziel | Messwert | erreicht |\n|---|---|---:|:-:|")
        w(zeile("Projekt Top-1", "≥ 95 %", pct(t1, n), n and t1 / n >= 0.95))
        w(zeile("Ordner Top-3", "≥ 85 %", pct(kz["top3"], nn), nn and kz["top3"] / nn >= 0.85))
        w(zeile("Ordner Top-1", "≥ 65 %", pct(kz["top1"], nn), nn and kz["top1"] / nn >= 0.65))
        w(zeile("sicher-falsch (der „eindeutigen\")", "≤ 3 %", f"{pct(kz['sicher_falsch'], ee)} (von {ee})", sf <= 0.03))
        w(zeile("`sicher_bis` korrekt", "≥ 95 %", pct(kz["sicher_ok"], nn), nn and kz["sicher_ok"] / nn >= 0.95))
        w(zeile("Laufzeit Ordner-Teil p95", "< 300 ms", f"{p95(ms_o):.0f} ms", p95(ms_o) < 300))
        w(zeile("Laufzeit Projekt p95 (inkl. Volltext-Rückfall)", "< 300 ms", f"{p95(ms_p):.0f} ms", p95(ms_p) < 300))
        w("")
        # Ganze Stichprobe zum Vergleich
        zs_all = auswerten_ordner(ctx, zeilen_neu, par, False)
        k = kennzahlen(zs_all)
        w(f"### Zum Vergleich: ganze Stichprobe, Temperatur {t_best} (teils im Training gesehen)\n")
        w(f"Top-1 {pct(k['top1'], k['n'])} · Top-3 {pct(k['top3'], k['n'])} · eindeutig {pct(k['eindeutig'], k['n'])} · "
          f"sicher-falsch {pct(k['sicher_falsch'], k['eindeutig'])}\n")

    bericht = "\n".join(out)
    print(bericht)
    ziel = Path(args.ausgabe or f"ablage_messung_{jetzt:%Y-%m-%d}.md")
    ziel.write_text(bericht, encoding="utf-8")
    print(f"\nBericht geschrieben: {ziel}", file=sys.stderr)


if __name__ == "__main__":
    main()
