"""Projekt bestimmen: Signale sammeln, Punkte vergeben, per Softmax in Wahrscheinlichkeiten übersetzen.

Gewichte (Start, Auftrag §6.2): Vorgänger sehr hoch, Projektnummer sehr hoch, Nummer UND Name im
Plankopf hoch, Name im Dateinamen mittel, Mail-Domain mittel, Volltext-Rückfall niedrig, Aktivität und
Rechner nur als leichter Prior. Jeder Punkt hat einen Grund in Klartext.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from scanner.ablage import merkmale as mk
from scanner.ablage.kontext import Kontext, tage_seit
from scanner.ablage.normalisieren import label_text

P_VORGAENGER = 10.0
P_NUMMER = 8.0
P_PLANKOPF = 5.0
P_PLANKOPF_NAME = 2.0
P_NAME_DATEINAME = 3.0
P_DOMAIN = 3.0
P_FTS = 2.0
P_AKTIV = 0.5
P_HOST = 0.5
FTS_NUR_BIS = 3.0          # Volltext-Rückfall nur, wenn kein stärkeres Signal da ist

_WORT4 = re.compile(r"[^\W\d_]{4,}", re.UNICODE)


@dataclass
class DateiInfo:
    dateiname: str
    groesse: int | None = None
    mtime: object = None
    hash: str | None = None
    text: str | None = None
    mail: dict | None = None
    host: str | None = None
    merkmale: dict = field(default_factory=dict)


@dataclass
class ProjektKandidat:
    id: int
    name: str
    p: float
    punkte: float
    gruende: list


def _softmax(punkte: dict[int, float]) -> dict[int, float]:
    if not punkte:
        return {}
    m = max(punkte.values())
    e = {k: math.exp(v - m) for k, v in punkte.items()}
    s = sum(e.values())
    return {k: v / s for k, v in e.items()}


def projekt_bestimmen(ctx: Kontext, datei: DateiInfo, top: int = 5) -> list[ProjektKandidat]:
    if not ctx.projekte:
        return []
    punkte: dict[int, float] = {pid: 0.0 for pid in ctx.projekte}
    gruende: dict[int, list] = {pid: [] for pid in ctx.projekte}

    def geben(pid, pkt, grund=None):
        if pid in punkte:
            punkte[pid] += pkt
            if grund and grund not in gruende[pid]:
                gruende[pid].append(grund)

    f = datei.merkmale
    datei_tokens = set(mk._tokens(mk._stamm_name(datei.dateiname)))

    # Vorgänger (strikt: gleicher Name ausser Datum) irgendwo im Bestand
    if ctx.vorgaenger_fn:
        vname = mk.vorgaenger_name(datei.dateiname)
        treffer = ctx.vorgaenger_fn(vname, None)
        pids = {t[0] for t in treffer}
        for pid, _oid, _pfad, fn in treffer:
            # Gleichnamige Dateien in mehreren Projekten (image001.png, Anhänge) sagen nichts über EIN Projekt
            geben(pid, P_VORGAENGER / len(pids), f"Vorgänger „{fn}“ liegt in diesem Projekt")

    # Führende Projektnummer (der Merkmals-Code hat sie schon gegen echte Projekte geprüft)
    for k in f:
        if k.startswith("proj:"):
            pid = ctx.projekt_zu_nummer(k[5:])
            if pid:
                geben(pid, P_NUMMER, f"Projektnummer {k[5:]} im Dateinamen")

    # Dateiname enthält unterscheidenden Namensbestandteil
    for pid, p in ctx.projekte.items():
        treffer = p.tokens & datei_tokens
        if treffer:
            geben(pid, P_NAME_DATEINAME, f"„{sorted(treffer)[0]}“ im Dateinamen")

    # Plankopf: Nummer UND Name in den ersten 3000 Zeichen
    if datei.text:
        kopf = set(label_text(datei.text[:mk.INHALT_ZEICHEN]).split())
        for pid, p in ctx.projekte.items():
            name_ok = p.tokens & kopf
            if not name_ok:
                continue
            if p.nummer is not None and str(p.nummer) in kopf:
                geben(pid, P_PLANKOPF, f"Projektnummer und Name „{sorted(name_ok)[0]}“ im Plankopf")
            else:
                geben(pid, P_PLANKOPF_NAME, f"Name „{sorted(name_ok)[0]}“ im Text")

    # Mail-Absender
    dom = next((k[4:] for k in f if k.startswith("dom:")), None)
    if dom and ctx.domain_fn:
        for pid, anteil in ctx.domain_fn(dom).items():
            geben(pid, P_DOMAIN * anteil, f"Absender {dom} schreibt meist zu diesem Projekt")

    # Volltext-Rückfall (nur Top-3 mit Anteil, nur wenn sonst nichts Starkes da ist)
    if datei.text and ctx.fts_fn and max(punkte.values()) < FTS_NUR_BIS:
        woerter, gesehen = [], set()
        for w in _WORT4.findall(datei.text[:mk.INHALT_ZEICHEN].lower()):
            if w not in gesehen and w not in mk.woerterbuch().stoppwoerter:
                gesehen.add(w)
                woerter.append(w)
            if len(woerter) >= 40:
                break
        if woerter:
            for pid, anteil in ctx.fts_fn(woerter).items():
                geben(pid, P_FTS * anteil, "Inhalt passt zu bisherigen Dokumenten")

    # Prior
    for pid, p in ctx.projekte.items():
        t = tage_seit(p.letzte_aenderung, ctx.jetzt)
        if p.aktiv and t is not None and t <= 30:
            geben(pid, P_AKTIV)
    if datei.host and ctx.host_fn:
        for pid, anteil in ctx.host_fn(datei.host).items():
            geben(pid, P_HOST * anteil)

    wk = _softmax(punkte)
    rang = sorted(wk, key=lambda k: -wk[k])[:top]
    return [ProjektKandidat(pid, ctx.projekte[pid].name, wk[pid], punkte[pid], gruende[pid][:3]) for pid in rang]
