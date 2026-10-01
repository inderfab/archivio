"""Daten, mit denen die Vorschlags-Engine rechnet — unabhängig davon, woher sie kommen.

Produktion: `lade_kontext(conn)` liest `ablage_ordner`, `ablage_slot`, `ablage_stats`.
Messung: `scripts/ablage_messung.py` baut denselben `Kontext` im Speicher (mit Zeit-Trennung).
Die Engine (`projekt.py`, `ordner.py`, `vorschlag.py`) kennt nur diese Klassen. Der Vorschlagspfad
fasst das NAS nie an: alles kommt aus der DB (Auftrag §0).
"""
from __future__ import annotations

import math
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import settings
from scanner.ablage import vorlage
from scanner.ablage.normalisieren import pfad_schluessel

N = "_n"


@dataclass
class Parameter:
    """Alle Stellschrauben der Engine. Startwerte aus dem Auftrag; getuned wird nur gegen die Messung."""
    alpha: float = 5.0          # Gewicht des Slots beim Ordner (Back-off)
    beta: float = 3.0           # ... der Rolle beim Slot
    gamma: float = 1.0          # ... von global bei der Rolle
    namens_gewicht: float = 1.5  # Bonus je Wort, das Dateiname und Ordnername teilen (0 = aus)
    idf_max: float = 0.7        # Obergrenze des idf-Gewichts (seltene Merkmale dürfen nicht alles überstimmen)
    delta: float = 4.0          # Gewicht des Elternordner-Teilbaums (0 = aus)
    temperatur: float = 3.0     # Softmax-Temperatur der Ordner-Scores (kalibriert in der Messung)
    eindeutig_min_n: float = 3.0  # „eindeutig" aus der Statistik nur mit so viel Vorgeschichte im Ordner selbst
    schwelle_sicher: float = 0.80
    min_option_p: float = 0.08  # Optionen unterhalb sind keine Optionen mehr
    projekt_sicher: float = 0.60
    prior_k: float = 40.0       # Gewicht des Slot-Anteils im Ordner-Prior
    aktivitaet_halbwertszeit_tage: float = 60.0
    neu_projekt_dateien: int = 200   # darunter gilt ein Projekt als neu (Vorlagen-Bonus)
    vorlagen_bonus: float = 2.0
    gewichte: dict = field(default_factory=lambda: {
        "ext": 1.0, "tok": 1.0, "plantyp": 2.0, "doktyp": 2.0, "bkp": 2.0, "fp": 2.0,
        "dom": 3.0, "phase": 1.5, "phasenr": 0.5, "plannr": 2.0, "geschoss": 1.0, "inh": 0.3,
        "flag": 0.5, "proj": 0.0})

    @classmethod
    def aus_config(cls) -> "Parameter":
        p = cls()
        for feld in ("alpha", "beta", "gamma", "temperatur", "schwelle_sicher", "projekt_sicher"):
            v = settings.get(f"ablage.{feld}")
            if v not in (None, ""):
                try:
                    setattr(p, feld, float(v))
                except (TypeError, ValueError):
                    pass
        return p


@dataclass
class Projekt:
    id: int
    name: str
    path: str
    nummer: int | None = None
    aktiv: bool = True
    letzte_aenderung: str | None = None
    tokens: frozenset = frozenset()          # unterscheidende Namensbestandteile ("emmenhof")


@dataclass
class OrdnerInfo:
    id: int
    project_id: int
    path: str
    rel_path: str
    name: str
    label: str
    parent_id: int | None
    art: str
    slot_id: int | None
    datei_anzahl: int = 0
    letzte_aenderung: str | None = None


@dataclass
class Slot:
    id: int
    label_pfad: str
    rolle: str
    anzeige: str
    aus_vorlage: bool = False
    abdeckung: float = 0.0


@dataclass
class Kontext:
    projekte: dict[int, Projekt] = field(default_factory=dict)
    ordner: dict[int, OrdnerInfo] = field(default_factory=dict)
    slots: dict[int, Slot] = field(default_factory=dict)
    stats: dict[tuple, dict] = field(default_factory=dict)      # (ebene, key_id) → {merkmal: gewicht}
    regeln: list[tuple[str, str]] = field(default_factory=list)  # (merkmal, slot-label_pfad)
    jetzt: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Abfragen, die nicht im Speicher liegen (Produktion: SQL; Messung: Zeit-getrennte Kopie)
    vorgaenger_fn: object = None      # (vorgaenger_name, projekt_id|None) → [(projekt_id, ordner_id, pfad, dateiname)]
    duplikat_fn: object = None        # hash → [(projekt_id, pfad)]
    fts_fn: object = None             # [wort] → {projekt_id: anteil}
    domain_fn: object = None          # domain → {projekt_id: anteil}
    host_fn: object = None            # host → {projekt_id: anteil}
    # abgeleitet
    _kinder: dict = field(default_factory=dict, repr=False)
    _je_projekt: dict = field(default_factory=dict, repr=False)
    _slot_nach_lp: dict = field(default_factory=dict, repr=False)
    _idf: dict = field(default_factory=dict, repr=False)
    _nummern: dict = field(default_factory=dict, repr=False)

    def fertig(self) -> "Kontext":
        """Abgeleitete Strukturen aufbauen; nach dem Befüllen einmal aufrufen."""
        self._kinder = defaultdict(list)
        self._je_projekt = defaultdict(list)
        for o in self.ordner.values():
            self._kinder[o.parent_id].append(o.id)
            self._je_projekt[o.project_id].append(o.id)
        self._slot_nach_lp = {s.label_pfad: s for s in self.slots.values()}
        self._nummern = {p.nummer: p.id for p in sorted(self.projekte.values(), key=lambda p: p.id)
                         if p.nummer is not None}
        # Unterscheidende Namensbestandteile: Wörter, die nur in EINEM Projektnamen vorkommen
        häufig: dict[str, int] = defaultdict(int)
        woerter = {pid: _namens_woerter(p.name) for pid, p in self.projekte.items()}
        for ws in woerter.values():
            for w in ws:
                häufig[w] += 1
        for pid, p in self.projekte.items():
            p.tokens = frozenset(w for w in woerter[pid] if häufig[w] == 1)
        # idf über die Slots: Merkmale, die überall vorkommen, tragen nichts bei
        df: dict[str, int] = defaultdict(int)
        n_slots = 0
        for (ebene, _), z in self.stats.items():
            if ebene == "slot":
                n_slots += 1
                for m in z:
                    if m != N:
                        df[m] += 1
        self._idf = {m: max(0.02, math.log((n_slots + 1) / (c + 1))) for m, c in df.items()} if n_slots else {}
        return self

    def teilbaum(self, projekt_id: int) -> dict:
        """Je Ordner die Merkmalszählung seines ganzen Teilbaums (er selbst plus alle Unterordner).
        Gibt neuen Unterordnern (datierte Sitzungsordner, neue Themen) Rückhalt vom Elternordner."""
        cache = self.__dict__.setdefault("_teilbaum", {})
        if projekt_id in cache:
            return cache[projekt_id]
        res: dict[int, dict] = {}
        for oid in self.ordner_von(projekt_id):
            z = self.stats.get(("ordner", oid))
            if not z:
                continue
            o = self.ordner[oid]
            while o is not None:
                ziel = res.setdefault(o.id, {})
                for m, g in z.items():
                    ziel[m] = ziel.get(m, 0.0) + g
                o = self.ordner.get(o.parent_id) if o.parent_id is not None else None
        cache[projekt_id] = res
        return res

    def namensindex(self, projekt_id: int) -> tuple[dict, dict]:
        """Wörter der Ordnernamen eines Projekts: (Wort → {Ordner-ID: Gewicht}, Wort → Seltenheit).
        Eigener Name zählt voll, Namen der Elternordner halb: wer in „…Kardex…" ablegt, legt auch unter dem
        Elternordner ab. Seltene Wörter tragen mehr als häufige („archiv", „planstaende")."""
        cache = self.__dict__.setdefault("_namensindex", {})
        if projekt_id in cache:
            return cache[projekt_id]
        from scanner.ablage.normalisieren import label_text
        index: dict[str, dict[int, float]] = defaultdict(dict)
        eigene: dict[int, set] = {}
        for oid in self.ordner_von(projekt_id):
            o = self.ordner[oid]
            if o.art != "normal":
                continue
            eigene[oid] = {w for w in label_text(o.name).split() if len(w) >= 4 and not w.isdigit()}
        for oid, ws in eigene.items():
            for w in ws:
                index[w][oid] = 1.0
        for oid in eigene:
            p = self.ordner.get(self.ordner[oid].parent_id) if self.ordner[oid].parent_id is not None else None
            tiefe = 0
            while p is not None and tiefe < 6:
                for w in eigene.get(p.id, ()):
                    index[w].setdefault(oid, 0.5)
                p = self.ordner.get(p.parent_id) if p.parent_id is not None else None
                tiefe += 1
        n = max(2, len(eigene))
        selten = {w: max(0.0, math.log(n / max(1, sum(1 for v in d.values() if v == 1.0)))) / math.log(n)
                  for w, d in index.items()}
        cache[projekt_id] = (dict(index), selten)
        return cache[projekt_id]

    def idf(self, merkmal: str) -> float:
        return self._idf.get(merkmal, 1.0)

    def kinder(self, parent_id):
        return self._kinder.get(parent_id, [])

    def ordner_von(self, projekt_id: int) -> list[int]:
        return self._je_projekt.get(projekt_id, [])

    def projekt_zu_nummer(self, nummer) -> int | None:
        try:
            return self._nummern.get(int(nummer))
        except (TypeError, ValueError):
            return None

    def slot_nach_pfad(self, label_pfad: str) -> Slot | None:
        return self._slot_nach_lp.get(label_pfad)


_STOPP_NAME = {"und", "der", "die", "das", "von", "mit", "umbau", "neubau", "projekt", "areal", "haus",
               "strasse", "weg", "platz"}


def _namens_woerter(name: str) -> set[str]:
    from scanner.ablage.normalisieren import label_text
    return {w for w in label_text(name).split()
            if len(w) >= 5 and not w.isdigit() and w not in _STOPP_NAME}


# ── Produktion: Laden aus der Datenbank ────────────────────────────────────────

_cache_lock = threading.Lock()
_cache: dict = {"ctx": None, "stempel": None}


def invalidieren() -> None:
    """Nach Scan, Strukturberechnung oder Statistik-Neuaufbau aufrufen."""
    with _cache_lock:
        _cache["ctx"] = None


def _stempel(conn) -> tuple:
    return (conn.execute("SELECT COUNT(*) FROM ablage_ordner").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM ablage_slot").fetchone()[0],
            conn.execute("SELECT COALESCE(SUM(gewicht), 0) FROM ablage_stats WHERE ebene='global' AND merkmal='_n'"
                         ).fetchone()[0],
            conn.execute("SELECT COALESCE(MAX(zuletzt_gesehen), '') FROM ablage_ordner").fetchone()[0])


def holen(conn) -> Kontext:
    """Gecachter Kontext der Produktion. Das Laden braucht ein, zwei Sekunden, der Vorschlag selbst
    soll nur noch rechnen. Der Stempel fängt Änderungen ab, die niemand `invalidieren()` gemeldet hat."""
    stempel = _stempel(conn)
    with _cache_lock:
        if _cache["ctx"] is not None and _cache["stempel"] == stempel:
            return _cache["ctx"]
    ctx = lade_kontext(conn)
    with _cache_lock:
        _cache["ctx"], _cache["stempel"] = ctx, stempel
    return ctx


def lade_kontext(conn, jetzt: datetime | None = None) -> Kontext:
    from scanner.ablage import produktion
    ctx = Kontext(jetzt=jetzt or datetime.now(timezone.utc))
    for r in conn.execute("SELECT id, name, path, active FROM projects"):
        if r["path"].startswith("mailbox:") or vorlage.ist_musterprojekt(r["path"]):
            continue
        ctx.projekte[r["id"]] = Projekt(r["id"], r["name"], r["path"], vorlage.projekt_nummer(r["name"], r["path"]),
                                        bool(r["active"]))
    for r in conn.execute("SELECT id, label_pfad, rolle, anzeige, aus_vorlage, abdeckung FROM ablage_slot"):
        ctx.slots[r["id"]] = Slot(r["id"], r["label_pfad"], r["rolle"], r["anzeige"], bool(r["aus_vorlage"]),
                                  r["abdeckung"])
    letzte: dict[int, str] = {}
    for r in conn.execute("SELECT id, project_id, path, rel_path, name, label, parent_id, art, slot_id, datei_anzahl,"
                          " letzte_aenderung FROM ablage_ordner"):
        if r["project_id"] not in ctx.projekte:
            continue
        ctx.ordner[r["id"]] = OrdnerInfo(r["id"], r["project_id"], r["path"], r["rel_path"], r["name"], r["label"],
                                         r["parent_id"], r["art"], r["slot_id"], r["datei_anzahl"],
                                         r["letzte_aenderung"])
        if r["letzte_aenderung"] and r["letzte_aenderung"] > letzte.get(r["project_id"], ""):
            letzte[r["project_id"]] = r["letzte_aenderung"]
    for pid, ts in letzte.items():
        ctx.projekte[pid].letzte_aenderung = ts
    for r in conn.execute("SELECT ebene, key_id, merkmal, gewicht FROM ablage_stats"):
        ctx.stats.setdefault((r["ebene"], r["key_id"]), {})[r["merkmal"]] = r["gewicht"]
    try:
        ctx.regeln = [(r["merkmal"], r["slot_label_pfad"]) for r in conn.execute(
            "SELECT merkmal, slot_label_pfad FROM ablage_regel WHERE aktiv = 1")]
    except Exception:
        ctx.regeln = []
    produktion.haken_setzen(ctx, conn)
    return ctx.fertig()


def tage_seit(iso: str | None, jetzt: datetime) -> float | None:
    from scanner.ablage import merkmale as mk
    d = mk._parse_mtime(iso)
    return None if d is None else max(0.0, (jetzt - d).total_seconds() / 86400)


def projekt_pfad_schluessel(p: Projekt) -> str:
    return pfad_schluessel(p.path)
