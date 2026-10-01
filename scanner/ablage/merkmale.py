"""Merkmale einer Datei für den Ablage-Vorschlag: Zählen, Regex und Wörterbücher, deterministisch.

`merkmale(...)` liefert `{merkmal: wert}` mit Präfixen (`ext:`, `tok:`, `plantyp:` …, siehe
planung/datei-drop-ablage.md §6.1). Dieselbe Funktion läuft beim Statistik-Aufbau (nur Name,
Endung, Mail-Metadaten) und beim Drop (zusätzlich die ersten ~3000 Zeichen Inhalt).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import yaml

from config import settings
from scanner.ablage.normalisieren import label_text

_WB_PFAD = Path(__file__).resolve().parent.parent.parent / "config" / "ablage_woerterbuch.yaml"
INHALT_ZEICHEN = 3000
_ENDUNG_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

_DATUM_RES = [
    re.compile(r"(?<!\d)(?:19|20)\d{2}[.\-/_](?:0[1-9]|1[0-2])[.\-/_](?:0[1-9]|[12]\d|3[01])(?!\d)"),   # yyyy-mm-dd
    re.compile(r"(?<!\d)(?:0[1-9]|[12]\d|3[01])\.(?:0[1-9]|1[0-2])\.(?:19|20)\d{2}(?!\d)"),            # dd.mm.yyyy
    re.compile(r"(?<!\d)(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d)"),                # yyyymmdd
    re.compile(r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d)"),                         # yymmdd
]
_KOPIE_RE = re.compile(r"(?:\s*\(\d+\)|[\s_\-]*(?:kopie|copy)(?:\s*\d+)?)+$", re.IGNORECASE)
# Ein einzelner Buchstabe gilt nur nach `_` oder `-` als Index (`GR_EG_b`); nach einem Leerzeichen
# ist er Teil des Namens („Haus A" / „Haus B" sind keine Versionen voneinander).
_INDEX_RE = re.compile(
    r"(?:[_\-]+[a-z]|[\s_\-.]+(?:rev\.?\s?\d+|index\s?[a-z0-9]+|v\d+(?:\.\d+)?))$", re.IGNORECASE)
_BKP_KONTEXT_RE = re.compile(r"\bbkp[\s_\-.:]*([1-9]\d{2}(?:[._]\d{1,2}(?!\d))?)", re.IGNORECASE)
_GESCHOSS_RES = [
    (re.compile(r"(?<![a-z0-9])(?:ug|eg|dg|attika)(?![a-z0-9])"), None),
    (re.compile(r"(?<![a-z0-9])(\d)\.?\s?og(?![a-z0-9])"), "{}og"),
    (re.compile(r"(?<![a-z0-9])og\s?(\d)(?![a-z0-9])"), "{}og"),
]
_MASSSTAB_RE = re.compile(r"(?<![a-z0-9])(?:1\s?[:_\-]\s?|m)(\d{2,4})(?![a-z0-9])")


# ── Wörterbuch ─────────────────────────────────────────────────────────────────

@dataclass
class Woerterbuch:
    stoppwoerter: frozenset
    plan_endungen: frozenset
    dokument_endungen: frozenset
    kategorien: dict = field(default_factory=dict)       # name → {schluessel: [woerter]}
    massstaebe: frozenset = frozenset()
    projektnummer: re.Pattern = field(default_factory=lambda: re.compile(r"\d{3}"))
    bkp_woerter: dict = field(default_factory=dict)      # Wort → BKP-Code, aus den Ordnernamen gelernt

    def mit_bkp_woertern(self, woerter: dict) -> "Woerterbuch":
        return Woerterbuch(self.stoppwoerter, self.plan_endungen, self.dokument_endungen, self.kategorien,
                           self.massstaebe, self.projektnummer, dict(woerter))


def _merge(basis: dict, zusatz: dict) -> dict:
    res = dict(basis)
    for k, v in (zusatz or {}).items():
        if isinstance(v, dict) and isinstance(res.get(k), dict):
            m = {kk: list(vv) for kk, vv in res[k].items()}
            for kk, vv in v.items():
                m[kk] = list(dict.fromkeys(m.get(kk, []) + list(vv)))
            res[k] = m
        elif isinstance(v, list) and isinstance(res.get(k), list):
            res[k] = list(dict.fromkeys(res[k] + v))
        else:
            res[k] = v
    return res


def _norm_wort(w) -> str:
    return label_text(str(w))


@lru_cache(maxsize=4)
def _wb_cached(zusatz_key: str) -> Woerterbuch:
    import json
    with open(_WB_PFAD, encoding="utf-8") as f:
        roh = yaml.safe_load(f) or {}
    roh = _merge(roh, json.loads(zusatz_key))
    kat = {n: {k: [_norm_wort(w) for w in ws] for k, ws in roh.get(n, {}).items()}
           for n in ("phase", "plantyp", "doktyp", "fachplaner")}
    try:
        pn = re.compile(roh.get("projektnummer") or r"\d{3}")
    except re.error:
        pn = re.compile(r"\d{3}")
    if settings.get("ablage.dateiname_projektnummer"):
        try:
            pn = re.compile(settings.get("ablage.dateiname_projektnummer"))
        except re.error:
            pass
    return Woerterbuch(
        stoppwoerter=frozenset(_norm_wort(w) for w in roh.get("stoppwoerter", [])),
        plan_endungen=frozenset(roh.get("plan_endungen", [])),
        dokument_endungen=frozenset(roh.get("dokument_endungen", [])),
        kategorien=kat,
        massstaebe=frozenset(str(m) for m in roh.get("massstaebe", [])),
        projektnummer=pn,
    )


def woerterbuch() -> Woerterbuch:
    """Standard-Wörterbuch plus `ablage.woerterbuch` aus config.yaml (ergänzt, ersetzt nie)."""
    import json
    zusatz = settings.get("ablage.woerterbuch") or {}
    return _wb_cached(json.dumps(zusatz, sort_keys=True, default=str))


def bkp_woerter_aus_ordnern(zeilen) -> dict[str, str]:
    """BKP-Bezeichnungen aus den Ordnernamen lernen: `baumeisterarbeiten` → `211`.

    `zeilen`: Paare (label, codes-JSON). Pro Wort gewinnt der häufigste erste Code; Wörter, die zu
    mehreren Codes gleich oft gehören, gelten als mehrdeutig und fallen weg.
    """
    import json
    from collections import Counter, defaultdict
    zaehler: dict[str, Counter] = defaultdict(Counter)
    for label, codes in zeilen:
        try:
            cl = json.loads(codes) if isinstance(codes, str) else list(codes)
        except ValueError:
            continue
        if not cl or "-" in cl[0]:
            continue
        code = cl[0].split(".")[0]
        for w in label.split():
            if len(w) >= 6 and not w.isdigit():
                zaehler[w][code] += 1
    res = {}
    for w, c in zaehler.items():
        top = c.most_common(2)
        if len(top) == 1 or top[0][1] > top[1][1]:
            res[w] = top[0][0]
    return res


# ── Hilfen ─────────────────────────────────────────────────────────────────────

def _stamm_name(dateiname: str) -> str:
    """Dateiname ohne Endung. Mails ohne Endung (Betreff) bleiben unverändert."""
    n = unicodedata.normalize("NFC", dateiname)
    return _ENDUNG_RE.sub("", n) if _ENDUNG_RE.search(n) and not n.endswith(".") else n


def _endung(dateiname: str) -> str:
    m = _ENDUNG_RE.search(dateiname)
    return m.group(0).lower() if m else ""


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(label_text(text))


def _passt(wort: str, token: str) -> bool:
    if len(wort) <= 3:
        return token == wort
    if token.startswith(wort):
        return True
    return len(wort) >= 6 and wort in token


def _treffer(kat: dict, tokens: list[str]) -> dict[str, bool]:
    """Schlüssel der Kategorie → ob ein Wort passt. Mehrwort-Einträge passen als Tokenfolge."""
    res = {}
    for key, woerter in kat.items():
        for w in woerter:
            if " " in w:
                teile = w.split()
                if any(tokens[i:i + len(teile)] == teile for i in range(len(tokens) - len(teile) + 1)):
                    res[key] = True
                    break
            elif any(_passt(w, t) for t in tokens):
                res[key] = True
                break
    return res


def datum_im_text(text: str) -> list[str]:
    return [m.group(0) for rx in _DATUM_RES for m in rx.finditer(text)]


def _ohne_datum(text: str) -> str:
    for rx in _DATUM_RES:
        text = rx.sub(" ", text)
    return text


def stamm(dateiname: str) -> str:
    """Stamm für die Vorgänger-Erkennung: normalisierter Name ohne Index/Revision, Datum,
    Kopie-Suffixe und Endung. `211_GR_EG_b.pdf` und `211_GR_EG_c.pdf` teilen einen Stamm."""
    s = _stamm_name(dateiname).strip().lower()
    s = _KOPIE_RE.sub("", s)
    s = _ohne_datum(s)
    for _ in range(2):
        neu = _INDEX_RE.sub("", s.strip())
        if neu == s.strip():
            break
        s = neu
    return " ".join(_tokens(s))


def hat_index(dateiname: str) -> bool:
    s = _KOPIE_RE.sub("", _ohne_datum(_stamm_name(dateiname).strip().lower())).strip()
    return bool(_INDEX_RE.search(s))


def _projektnummer_kandidat(roh_tokens: list[str], pn: re.Pattern, bkp_codes: set[str]) -> str | None:
    """Erstes Wort, oder das Wort direkt nach einem Datum (`260320_215_WB`): passt es zur
    Projektnummern-Form und gehört es nicht zu einem BKP-Kontext, ist es der Kandidat."""
    def ist_datum(t: str) -> bool:
        return len(t) in (6, 8) and t.isdigit() and bool(datum_im_text(t))
    idx = 1 if roh_tokens and ist_datum(roh_tokens[0]) else 0
    if idx >= len(roh_tokens):
        return None
    t = roh_tokens[idx]
    if pn.fullmatch(t) and t not in bkp_codes:
        return t
    return None


def _parse_mtime(mtime) -> datetime | None:
    if isinstance(mtime, datetime):
        return mtime if mtime.tzinfo else mtime.replace(tzinfo=timezone.utc)
    if not mtime:
        return None
    try:
        d = datetime.fromisoformat(str(mtime).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ── Hauptfunktion ──────────────────────────────────────────────────────────────

def merkmale(dateiname: str, groesse: int | None = None, mtime=None, text: str | None = None,
             mail: dict | None = None, wb: Woerterbuch | None = None,
             bekannte_projekte: set[str] | None = None) -> dict[str, float]:
    """Alle Merkmale einer Datei.

    `bekannte_projekte`: Menge existierender Projektnummern. Ist sie gesetzt, zählt eine führende
    Nummer ohne passendes Projekt NICHT als `proj:` (Fachplaner-Auftragsnummer am Dateianfang).
    `groesse` und `mtime` sind für spätere Merkmale vorgesehen und beeinflussen heute nichts.
    """
    wb = wb or woerterbuch()
    m: dict[str, float] = {}
    name = unicodedata.normalize("NFC", dateiname or "")
    ext = _endung(name)
    if mail is not None and not ext:
        ext = ".eml"
    if ext:
        m[f"ext:{ext}"] = 1.0

    stem = _stamm_name(name)
    roh_tokens = _tokens(stem)
    stopp = wb.stoppwoerter
    nur_name = label_text(stem)

    # Mail: Betreff-Tokens und Absender-Domain
    if mail:
        absender = str(mail.get("sender") or mail.get("absender") or "")
        dom = re.search(r"@([A-Za-z0-9.\-]+)", absender)
        if dom:
            m[f"dom:{dom.group(1).lower().rstrip('.')}"] = 1.0
        betreff_tokens = _tokens(str(mail.get("subject") or mail.get("betreff") or ""))
        roh_tokens = list(dict.fromkeys(roh_tokens + betreff_tokens))

    # Tokens
    for t in roh_tokens:
        if len(t) >= 2 and not t.isdigit() and t not in stopp:
            m[f"tok:{t}"] = 1.0

    # BKP: nur mit Kontext (`BKP 211`) oder über gelernte Bezeichnungen
    bkp: set[str] = set()
    for mm in _BKP_KONTEXT_RE.finditer(name):
        bkp.add(mm.group(1).replace("_", ".").split(".")[0])
    for t in roh_tokens:
        code = wb.bkp_woerter.get(t)
        if code:
            bkp.add(code)
    for c in sorted(bkp):
        m[f"bkp:{c}"] = 1.0

    # Projektnummer (nur Kandidat; die Prüfung gegen echte Projekte macht der Projekt-Scorer)
    if not nur_name.startswith("bkp"):
        kand = _projektnummer_kandidat(roh_tokens, wb.projektnummer, bkp)
        if kand and (bekannte_projekte is None or kand in bekannte_projekte):
            m[f"proj:{kand}"] = 1.0

    # Kategorien
    for key in _treffer(wb.kategorien.get("phase", {}), roh_tokens):
        m[f"phase:{key}"] = 1.0
    for key in _treffer(wb.kategorien.get("doktyp", {}), roh_tokens):
        m[f"doktyp:{key}"] = 1.0
    for key in _treffer(wb.kategorien.get("fachplaner", {}), roh_tokens):
        m[f"fp:{key}"] = 1.0

    # Plan-Merkmale: Geschoss/Massstab/Kurzkürzel nur mit Plan-Kontext
    lraw = stem.lower()
    geschosse = set()
    for rx, fmt in _GESCHOSS_RES:
        for mm in rx.finditer(lraw):
            geschosse.add(fmt.format(mm.group(1)) if fmt else mm.group(0))
    plan_kat = wb.kategorien.get("plantyp", {})
    langform = _treffer({k: [w for w in ws if len(w) > 3] for k, ws in plan_kat.items()}, roh_tokens)
    vor_kontext = (bool(langform) or ext in wb.plan_endungen
                   or (bool(geschosse) and ext not in wb.dokument_endungen))
    massstab = any(mm.group(1) in wb.massstaebe for mm in _MASSSTAB_RE.finditer(lraw)) or (
        vor_kontext and any(t in wb.massstaebe for t in roh_tokens[1:]))
    plantypen = dict(langform)
    if vor_kontext or massstab:
        plantypen.update(_treffer(plan_kat, roh_tokens))     # jetzt auch Kurzkürzel (gr, sn, an …)
        for g in sorted(geschosse):
            m[f"geschoss:{g}"] = 1.0
        if massstab:
            m["massstab"] = 1.0
    for key in plantypen:
        m[f"plantyp:{key}"] = 1.0

    # Flags
    if datum_im_text(name):
        m["datum"] = 1.0
    if hat_index(name):
        m["index"] = 1.0

    # Inhalt (nur beim Drop): ersten Zeichen, niedrig gewichtet
    if text:
        ausschnitt = text[:INHALT_ZEICHEN]
        from collections import Counter
        z = Counter(t for t in _tokens(ausschnitt)
                    if len(t) >= 4 and not t.isdigit() and t not in stopp)
        for t, _ in z.most_common(40):
            m[f"inh:{t}"] = 1.0
    return m


def typ(merkmal: str) -> str:
    """Typ eines Merkmals für die Gewichtung (`tok:abc` → `tok`, `index` → `flag`)."""
    if ":" in merkmal:
        return merkmal.split(":", 1)[0]
    return "flag"
