"""Ablage-Vorschlag: Orchestrierung und Ausgabeformat (Auftrag §6.4/§6.5).

Reihenfolge: Duplikat → Projekt → Regeln → Vorgänger → Statistik (Naive Bayes mit Hierarchie-Konfidenz).
Jede Option trägt 1–3 Gründe in Klartext. Es wird nie etwas verschoben: das Ergebnis ist ein Vorschlag.
Der Pfad fasst das NAS nicht an; die Struktur kommt aus dem `Kontext` (DB).
"""
from __future__ import annotations

import time

from scanner.ablage import merkmale as mk
from scanner.ablage import ordner as od
from scanner.ablage import projekt as pj
from scanner.ablage.kontext import Kontext, Parameter
from scanner.ablage.projekt import DateiInfo

P_REGEL = 0.99
P_VORGAENGER = 0.97


def _absicht_label(wb: mk.Woerterbuch, k: od.Kand) -> str | None:
    lp = f" {k.label_pfad.replace('/', ' ')} " if k.label_pfad else f" {k.rolle} "
    for key in sorted(wb.absichten, key=len, reverse=True):
        if f" {key} " in lp:
            return wb.absichten[key]
    return None


def _option(ctx: Kontext, wb, k: od.Kand, p: float, gruende: list[str], **extra) -> dict:
    o = {"pfad": k.pfad, "p": round(p, 4), "label": _absicht_label(wb, k) or k.name,
         "gruende": gruende[:3], "neu_anlegen": k.virtuell, "vorgaenger": None, "archiv_ordner": None}
    o.update(extra)
    return o


def _archiv_kind(ctx: Kontext, ordner_id: int | None) -> str | None:
    if ordner_id is None:
        return None
    for kid in ctx.kinder(ordner_id):
        if ctx.ordner[kid].art == "archiv":
            return ctx.ordner[kid].path
    return None


def _gemeinsamer_ahne(ctx: Kontext, projekt_id: int, ordner_ids: list[int]) -> str:
    ketten = []
    for oid in ordner_ids:
        kette, o = [], ctx.ordner.get(oid)
        while o is not None:
            kette.append(o.id)
            o = ctx.ordner.get(o.parent_id) if o.parent_id is not None else None
        ketten.append(list(reversed(kette)))
    gem = None
    for stufe in zip(*ketten):
        if len(set(stufe)) == 1:
            gem = stufe[0]
        else:
            break
    return ctx.ordner[gem].path if gem is not None else ctx.projekte[projekt_id].path


def ordner_vorschlag(ctx: Kontext, datei: DateiInfo, projekt_id: int, par: Parameter | None = None,
                     mit_vorgaenger: bool = True) -> dict:
    """Ordner-Teil des Vorschlags für ein feststehendes Projekt. Rückgabe: fall, sicher_bis, optionen."""
    par = par or Parameter()
    wb = mk.woerterbuch()
    f = datei.merkmale
    projekt = ctx.projekte[projekt_id]
    wurzel = {"pfad": projekt.path, "p": 1.0}

    # 1. Explizite Regeln: Merkmal passt und der Slot existiert im Projekt
    for merkmal, lp in ctx.regeln:
        if merkmal in f:
            for k in od.kandidaten(ctx, projekt_id):
                if k.label_pfad == lp:
                    return {"fall": "eindeutig", "sicher_bis": {"pfad": k.pfad, "p": P_REGEL},
                            "optionen": [_option(ctx, wb, k, P_REGEL, [f"Regel: „{merkmal}“ gehört nach „{k.name}“"])]}

    # 2. Vorgänger (strikt: gleicher Name ausser Datum)
    if mit_vorgaenger and ctx.vorgaenger_fn:
        treffer = [t for t in ctx.vorgaenger_fn(mk.vorgaenger_name(datei.dateiname), projekt_id) if t[0] == projekt_id]
        if treffer:
            je_ordner: dict[int, tuple] = {}
            for _pid, oid, pfad, fn in treffer:
                je_ordner.setdefault(oid, (pfad, fn))
            ids = list(je_ordner)
            opts = []
            for oid in ids:
                o = ctx.ordner[oid]
                pfad, fn = je_ordner[oid]
                kand = od.Kand(oid, oid, o.slot_id, o.parent_id, o.path, o.name, o.label,
                               ctx.slots[o.slot_id].label_pfad if o.slot_id in ctx.slots else "")
                opts.append(_option(ctx, wb, kand, P_VORGAENGER / len(ids), [f"Vorgänger „{fn}“ liegt hier"],
                                    vorgaenger=pfad, archiv_ordner=_archiv_kind(ctx, oid)))
            if len(ids) == 1:
                return {"fall": "eindeutig", "sicher_bis": {"pfad": opts[0]["pfad"], "p": P_VORGAENGER},
                        "optionen": opts}
            return {"fall": "teilweise", "sicher_bis": {"pfad": _gemeinsamer_ahne(ctx, projekt_id, ids),
                                                          "p": P_VORGAENGER}, "optionen": opts[:3]}

    # 3. Statistik
    kands, scores = od.bewerten(ctx, projekt_id, f, par)
    if not kands:
        return {"fall": "ordner_unklar", "sicher_bis": wurzel, "optionen": []}
    probs = od.wahrscheinlichkeiten(scores, par.temperatur)
    h = od.hierarchie(ctx, projekt_id, kands, probs, par)
    opts = [_option(ctx, wb, k, p, od.gruende(ctx, par, k, f)) for k, p in h["optionen"]]
    return {"fall": h["fall"], "sicher_bis": {"pfad": h["sicher_pfad"], "p": round(h["sicher_p"], 4)},
            "optionen": opts}


def vorschlagen(ctx: Kontext, datei: DateiInfo, par: Parameter | None = None, projekt_id: int | None = None,
                mit_vorgaenger: bool = True) -> dict:
    """Vollständiger Vorschlag (Format §6.5). `projekt_id` erzwingt das Projekt (z. B. nach manueller Wahl)."""
    t0 = time.perf_counter()
    par = par or Parameter.aus_config()
    if not datei.merkmale:
        datei.merkmale = mk.merkmale(datei.dateiname, datei.groesse, datei.mtime, datei.text, datei.mail,
                                     bekannte_projekte={str(n) for n in ctx._nummern})
    res: dict = {"fall": "projekt_unklar", "projekte": [], "sicher_bis": None, "optionen": [],
                 "dateiname_vorschlag": None}

    # Duplikat: derselbe Inhalt liegt schon im Bestand
    if datei.hash and ctx.duplikat_fn:
        dup = ctx.duplikat_fn(datei.hash)
        if dup:
            res.update({"fall": "duplikat",
                        "duplikate": [{"projekt_id": pid, "pfad": pfad} for pid, pfad in dup[:5]]})
            res["dauer_ms"] = int((time.perf_counter() - t0) * 1000)
            return res

    if projekt_id is not None and projekt_id in ctx.projekte:
        res["projekte"] = [{"id": projekt_id, "name": ctx.projekte[projekt_id].name, "p": 1.0,
                            "gruende": ["Von Hand gewählt"]}]
        pid = projekt_id
    else:
        kandidaten = pj.projekt_bestimmen(ctx, datei)
        res["projekte"] = [{"id": c.id, "name": c.name, "p": round(c.p, 4), "gruende": c.gruende}
                           for c in kandidaten[:3]]
        if not kandidaten or kandidaten[0].p < par.projekt_sicher:
            res["dauer_ms"] = int((time.perf_counter() - t0) * 1000)
            return res
        pid = kandidaten[0].id

    teil = ordner_vorschlag(ctx, datei, pid, par, mit_vorgaenger)
    res.update(teil)
    res["dauer_ms"] = int((time.perf_counter() - t0) * 1000)
    return res
