"""Ordner bestimmen: Naive-Bayes über die Hierarchie mit Back-off und Hierarchie-Konfidenz.

Score je Kandidat (Log-Raum):

    score(o) = log Prior(o) + Σ_m  g(typ(m)) · idf(m) · log P(m | o)

`P(m | o)` fällt hierarchisch zurück: Ordner → Slot → Rolle → global. Wo ein Ordner wenig oder keine
eigenen Dateien hat (neues Projekt, leerer Vorlage-Ordner), trägt der Slot das Urteil. Die
Wahrscheinlichkeiten entstehen per Softmax über alle Kandidaten und werden im Ordnerbaum nach oben
summiert: `sicher_bis` ist der tiefste Knoten mit Summe ≥ Schwelle. Statt eines falschen Ordners
gibt es dann ehrlich „sicher bis hierher, darunter 2–3 Optionen".
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from scanner.ablage import merkmale as mk
from scanner.ablage.index import N, rolle_key
from scanner.ablage.kontext import Kontext, Parameter, tage_seit

_GLATT = 0.1             # Laplace-Glättung der globalen Wahrscheinlichkeit
_PLANTYP_NAMEN = {"GR": "Grundrisse", "SN": "Schnitte", "AN": "Ansichten", "DET": "Details",
                  "SIT": "Situationspläne", "UMG": "Umgebungspläne"}


@dataclass
class Kand:
    key: object                      # Ordner-ID (int) oder ("v", slot_id) bei virtuellen Ordnern
    ordner_id: int | None
    slot_id: int | None
    parent_key: object               # Key des Elternordners, None = Projektwurzel
    pfad: str
    name: str
    rolle: str
    label_pfad: str = ""
    virtuell: bool = False


# ── Kandidaten ─────────────────────────────────────────────────────────────────

def kandidaten(ctx: Kontext, projekt_id: int) -> list[Kand]:
    """Alle normalen Ordner des Projekts, plus virtuelle Slots der Vorlage, die im Projekt fehlen
    (nur wenn der Elternordner existiert). Archiv-, Trenner- und ausgeschlossene Ordner nie."""
    projekt = ctx.projekte[projekt_id]
    ordner = [ctx.ordner[i] for i in ctx.ordner_von(projekt_id)]
    res: list[Kand] = []
    slot_ordner: dict[int, int] = {}
    for o in ordner:
        if o.slot_id is not None and (o.slot_id not in slot_ordner
                                      or o.datei_anzahl > ctx.ordner[slot_ordner[o.slot_id]].datei_anzahl):
            slot_ordner[o.slot_id] = o.id
        if o.art != "normal":
            continue
        slot = ctx.slots.get(o.slot_id) if o.slot_id is not None else None
        res.append(Kand(o.id, o.id, o.slot_id, o.parent_id, o.path, o.name, o.label,
                        slot.label_pfad if slot else ""))
    for sid, slot in ctx.slots.items():
        if sid in slot_ordner or slot.rolle == "archiv":
            continue
        if not (slot.aus_vorlage or slot.abdeckung >= 0.5):
            continue
        eltern_lp = slot.label_pfad.rsplit("/", 1)[0] if "/" in slot.label_pfad else None
        if eltern_lp is None:
            eltern_key, eltern_pfad = None, projekt.path
        else:
            es = ctx.slot_nach_pfad(eltern_lp)
            if es is None or es.id not in slot_ordner:
                continue
            eo = ctx.ordner[slot_ordner[es.id]]
            if eo.art != "normal":
                continue
            eltern_key, eltern_pfad = eo.id, eo.path
        res.append(Kand(("v", sid), None, sid, eltern_key, f"{eltern_pfad.rstrip('/')}/{slot.anzeige}",
                        slot.anzeige, slot.rolle, slot.label_pfad, True))
    return res


# ── Wahrscheinlichkeiten mit Back-off ──────────────────────────────────────────

class _Modell:
    def __init__(self, ctx: Kontext, par: Parameter, projekt_id: int | None = None):
        self.ctx, self.par = ctx, par
        self.teilbaum = ctx.teilbaum(projekt_id) if projekt_id is not None else {}
        self.g = ctx.stats.get(("global", 0), {})
        self.Ng = self.g.get(N, 0.0)
        self.V = max(1, len(self.g) - 1)
        self._pg: dict = {}
        self._pr: dict = {}
        self._ps: dict = {}

    def pg(self, m: str) -> float:
        v = self._pg.get(m)
        if v is None:
            v = (self.g.get(m, 0.0) + _GLATT) / (self.Ng + _GLATT * self.V)
            self._pg[m] = v
        return v

    def pr(self, rolle: str, m: str) -> float:
        k = (rolle, m)
        v = self._pr.get(k)
        if v is None:
            z = self.ctx.stats.get(("rolle", rolle_key(rolle)))
            v = self.pg(m) if not z else (z.get(m, 0.0) + self.par.gamma * self.pg(m)) / (z.get(N, 0.0) + self.par.gamma)
            self._pr[k] = v
        return v

    def ps(self, slot_id, rolle: str, m: str) -> float:
        k = (slot_id, rolle, m)
        v = self._ps.get(k)
        if v is None:
            z = self.ctx.stats.get(("slot", slot_id)) if slot_id is not None else None
            base = self.pr(rolle, m)
            v = base if not z else (z.get(m, 0.0) + self.par.beta * base) / (z.get(N, 0.0) + self.par.beta)
            self._ps[k] = v
        return v

    def pt(self, k: Kand, m: str, base: float) -> float:
        """Rückhalt vom Elternordner: dessen Teilbaum-Statistik, über `base` geglättet."""
        if self.par.delta <= 0 or k.parent_key is None:
            return base
        z = self.teilbaum.get(k.parent_key)
        if not z:
            return base
        return (z.get(m, 0.0) + self.par.delta * base) / (z.get(N, 0.0) + self.par.delta)

    def po(self, k: Kand, m: str) -> float:
        base = self.pt(k, m, self.ps(k.slot_id, k.rolle, m))
        z = self.ctx.stats.get(("ordner", k.ordner_id)) if k.ordner_id is not None else None
        if not z:
            return base
        return (z.get(m, 0.0) + self.par.alpha * base) / (z.get(N, 0.0) + self.par.alpha)


def _aktivitaet(ctx: Kontext, projekt_id: int, par: Parameter) -> dict:
    """Je Top-Level-Ordner (Phase): 0.5 ** (Alter der jüngsten Änderung im Teilbaum / Halbwertszeit)."""
    jüngste: dict[int, str] = {}
    for oid in ctx.ordner_von(projekt_id):
        o = ctx.ordner[oid]
        if not o.letzte_aenderung:
            continue
        top = o
        while top.parent_id is not None and top.parent_id in ctx.ordner:
            top = ctx.ordner[top.parent_id]
        if o.letzte_aenderung > jüngste.get(top.id, ""):
            jüngste[top.id] = o.letzte_aenderung
    res = {}
    for tid, ts in jüngste.items():
        t = tage_seit(ts, ctx.jetzt)
        res[tid] = 0.5 ** ((t or 0) / par.aktivitaet_halbwertszeit_tage)
    return res


def bewerten(ctx: Kontext, projekt_id: int, features: dict, par: Parameter,
             kands: list[Kand] | None = None, vorgaenger_ordner=frozenset()) -> tuple[list[Kand], list[float]]:
    """Log-Scores aller Kandidaten (ohne Temperatur)."""
    kands = kands if kands is not None else kandidaten(ctx, projekt_id)
    if not kands:
        return [], []
    mod = _Modell(ctx, par, projekt_id)
    gew = par.gewichte
    merkm = [(m, gew.get(mk.typ(m), 1.0) * min(par.idf_max, ctx.idf(m))) for m in features
             if gew.get(mk.typ(m), 1.0) > 0 and mk.typ(m) != "inh"]
    n_projekt = sum(ctx.ordner[i].datei_anzahl for i in ctx.ordner_von(projekt_id))
    n_total = max(mod.Ng, 1.0)
    akt = _aktivitaet(ctx, projekt_id, par)
    neu = n_projekt < par.neu_projekt_dateien

    namen = namens_treffer(ctx, projekt_id, features) if par.namens_gewicht > 0 else {}
    scores = []
    for k in kands:
        o = ctx.ordner.get(k.ordner_id) if k.ordner_id is not None else None
        n_o = o.datei_anzahl if o else 0
        slot_z = ctx.stats.get(("slot", k.slot_id)) if k.slot_id is not None else None
        anteil = (slot_z.get(N, 0.0) / n_total) if slot_z else 0.0
        prior = (n_o + par.prior_k * anteil) / (n_projekt + par.prior_k) + 1e-9
        top = o if o else (ctx.ordner.get(k.parent_key) if k.parent_key is not None else None)
        while top is not None and top.parent_id is not None and top.parent_id in ctx.ordner:
            top = ctx.ordner[top.parent_id]
        prior *= 0.5 + akt.get(top.id, 0.5) if top is not None else 1.0
        if neu and n_o == 0 and k.slot_id is not None:
            prior *= par.vorlagen_bonus
        s = math.log(prior)
        for m, w in merkm:
            s += w * math.log(mod.po(k, m))
        if k.ordner_id in namen:
            s += par.namens_gewicht * namen[k.ordner_id][0]
        if k.ordner_id is not None and k.ordner_id in vorgaenger_ordner:
            s += par.vorgaenger_bonus
        scores.append(s)
    return kands, scores


def namens_treffer(ctx: Kontext, projekt_id: int, features: dict) -> dict:
    """Ordner-ID → (Bonus, Wörter): Dateiname und Ordnername teilen Wörter (auch in Zusammensetzungen:
    „kardexabschluesse" trifft „kardex")."""
    index, selten = ctx.namensindex(projekt_id)
    toks = [m[4:] for m in features if m.startswith("tok:") and len(m) >= 8]
    res: dict[int, list] = {}
    for u, ordner in index.items():
        for t in toks:
            if t == u or (len(u) >= 5 and t.startswith(u)) or (len(t) >= 5 and u.startswith(t)) or \
                    (len(u) >= 6 and u in t):
                for oid, g in ordner.items():
                    e = res.setdefault(oid, [0.0, []])
                    e[0] += g * selten.get(u, 0.0)
                    e[1].append(u)
                break
    return {oid: (min(b, 3.0), ws) for oid, (b, ws) in res.items()}


def wahrscheinlichkeiten(scores: list[float], temperatur: float) -> list[float]:
    if not scores:
        return []
    t = max(temperatur, 1e-6)
    mx = max(scores)
    e = [math.exp((s - mx) / t) for s in scores]
    z = sum(e)
    return [x / z for x in e]


# ── Hierarchie ─────────────────────────────────────────────────────────────────

def _kette(ctx: Kontext, key, kand_by_key: dict) -> list:
    """Schlüssel von `key` aufwärts bis zum obersten Ordner (inklusive)."""
    res = []
    k = key
    while k is not None:
        res.append(k)
        if k in kand_by_key:
            k = kand_by_key[k].parent_key
        else:
            o = ctx.ordner.get(k)
            k = o.parent_id if o else None
        if len(res) > 64:
            break
    return res


def _knoten_kand(ctx: Kontext, key, kand_by_key: dict) -> Kand:
    """Kand-Objekt für einen Knoten, der selbst kein Kandidat ist (Zwischenordner)."""
    if key in kand_by_key:
        return kand_by_key[key]
    o = ctx.ordner[key]
    return Kand(o.id, o.id, o.slot_id, o.parent_id, o.path, o.name, o.label,
                ctx.slots[o.slot_id].label_pfad if o.slot_id in ctx.slots else "")


def hierarchie(ctx: Kontext, projekt_id: int, kands: list[Kand], probs: list[float], par: Parameter) -> dict:
    """Wahrscheinlichkeiten im Ordnerbaum nach oben summieren.

    * `sicher_bis`: der tiefste Knoten mit Summe ≥ Schwelle (bei Schwelle > 0.5 ist das eine eindeutige Kette).
    * `eindeutig`: der beste Kandidat selbst hat ≥ Schwelle.
    * Optionen: die 2–3 wahrscheinlichsten Zweige unterhalb von `sicher_bis`, jeder so tief verfeinert,
      wie ein Unterordner dominiert (die Summe eines Zweigs gilt für den ganzen Teilbaum).
    """
    kand_by_key = {k.key: k for k in kands}
    marg: dict = {None: 1.0}
    kinder: dict = {}
    eigen: dict = {}
    for k, p in zip(kands, probs):
        eigen[k.key] = p
        kette = _kette(ctx, k.key, kand_by_key)
        for i, node in enumerate(kette):
            marg[node] = marg.get(node, 0.0) + p
            eltern = kette[i + 1] if i + 1 < len(kette) else None
            kinder.setdefault(eltern, set()).add(node)
    wurzel = ctx.projekte[projekt_id].path

    def beste_kinder(node):
        return sorted(kinder.get(node, ()), key=lambda c: -marg[c])

    rang = sorted(range(len(kands)), key=lambda i: -probs[i])
    best, best_p = kands[rang[0]], probs[rang[0]]
    hist = (ctx.stats.get(("ordner", best.ordner_id)) or {}).get(N, 0.0) if best.ordner_id is not None else 0.0
    if best_p >= par.schwelle_sicher and hist >= par.eindeutig_min_n:
        return {"fall": "eindeutig", "sicher_key": best.key, "sicher_pfad": best.pfad, "sicher_p": best_p,
                "optionen": [(best, best_p)]}

    sicher = None
    while True:
        kids = beste_kinder(sicher)
        if kids and marg[kids[0]] >= par.schwelle_sicher:
            sicher = kids[0]
        else:
            break

    def verfeinern(node):
        """Von `node` so weit absteigen, wie ein Unterordner den Zweig dominiert."""
        while True:
            kids = beste_kinder(node)
            if not kids or eigen.get(node, 0.0) >= 0.4 * marg[node] or marg[kids[0]] < 0.5 * marg[node]:
                return node
            node = kids[0]

    optionen = []
    for c in beste_kinder(sicher)[:3]:
        if marg[c] < par.min_option_p and optionen:
            break
        n = verfeinern(c)
        optionen.append((_knoten_kand(ctx, n, kand_by_key), marg[n]))
    if sicher is not None and eigen.get(sicher, 0.0) >= par.min_option_p:      # Dateien direkt im sicheren Ordner
        optionen.append((_knoten_kand(ctx, sicher, kand_by_key), eigen[sicher]))
        optionen.sort(key=lambda t: -t[1])
        optionen = optionen[:3]
    sicher_pfad = wurzel if sicher is None else _knoten_kand(ctx, sicher, kand_by_key).pfad
    return {"fall": "teilweise" if sicher is not None else "ordner_unklar", "sicher_key": sicher,
            "sicher_pfad": sicher_pfad, "sicher_p": marg[sicher], "optionen": optionen}


# ── Gründe in Klartext ─────────────────────────────────────────────────────────

def k_projekt(ctx: Kontext, k: Kand):
    o = ctx.ordner.get(k.ordner_id if k.ordner_id is not None else k.parent_key)
    return o.project_id if o else None


def gruende(ctx: Kontext, par: Parameter, k: Kand, features: dict, n: int = 3) -> list[str]:
    """Die stärksten Merkmale, die für diesen Ordner sprechen, als lesbare Sätze."""
    mod = _Modell(ctx, par, k_projekt(ctx, k))
    zo = ctx.stats.get(("ordner", k.ordner_id)) if k.ordner_id is not None else None
    zs = ctx.stats.get(("slot", k.slot_id)) if k.slot_id is not None else None
    anzeige = k.name
    beitraege = []
    for m in features:
        t = mk.typ(m)
        w = par.gewichte.get(t, 1.0)
        if w <= 0 or t in ("inh", "flag", "proj"):
            continue
        gewinn = w * ctx.idf(m) * (math.log(mod.po(k, m)) - math.log(mod.pg(m)))
        if gewinn > 0:
            anzahl = (zo or {}).get(m) or (zs or {}).get(m) or 0.0
            beitraege.append((gewinn, m, anzahl))
    beitraege.sort(reverse=True)
    res = []
    if k.ordner_id is not None and par.namens_gewicht > 0:
        pid = k_projekt(ctx, k)
        nt = namens_treffer(ctx, pid, features).get(k.ordner_id) if pid is not None else None
        if nt and nt[1]:
            res.append(f"Name passt zum Ordner: „{', '.join(sorted(set(nt[1]))[:2])}“")
    for _, m, anzahl in beitraege:
        t, _, wert = m.partition(":")
        a = f"{anzahl:.0f} " if anzahl >= 1.5 else ""
        if t == "plantyp":
            s = f"{a}{_PLANTYP_NAMEN.get(wert, wert)} liegen in „{anzeige}“"
        elif t == "doktyp":
            s = f"{a}{wert}-Dokumente liegen in „{anzeige}“"
        elif t == "tok":
            s = f"{a or 'Ähnliche '}Dateien mit „{wert}“ liegen in „{anzeige}“"
        elif t == "bkp":
            s = f"BKP {wert} gehört nach „{anzeige}“"
        elif t == "fp":
            s = f"Unterlagen dieses Fachplaners ({wert}) liegen in „{anzeige}“"
        elif t == "phase":
            s = f"Phase {wert} passt zu „{anzeige}“"
        elif t in ("plannr", "phasenr"):
            s = f"Plannummer {wert}… liegt in „{anzeige}“"
        elif t == "dom":
            s = f"Mails von {wert} liegen in „{anzeige}“"
        elif t == "geschoss":
            s = f"Pläne zu {wert.upper()} liegen in „{anzeige}“"
        elif t == "ext":
            s = f"{a}{wert}-Dateien liegen in „{anzeige}“"
        else:
            continue
        res.append(s)
        if len(res) >= n:
            break
    if not res and k.virtuell:
        res.append("Gehört nach der Vorlage zu diesem Projekt")
    return res
