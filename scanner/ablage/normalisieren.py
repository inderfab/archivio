"""Ordnernamen zerlegen: Ordnungspräfix, normalisiertes Label, Codes.

`zerlege("51a_Planstände")` → ("51a", "planstaende", [])
`zerlege("201_209_Baugrubenaushub")` → ("", "baugrubenaushub", ["201-209"])

Das Label (nicht Präfix, nicht Code) ist der Schlüssel für Slots: dieselbe Rolle
trägt je Phase einen anderen Buchstaben (`31d_Fachplaner`, `51f_Fachplaner`), und
derselbe BKP-Code kann zwei verschiedene Ordner bezeichnen (`273.4 Türen in Holz`
gegenüber `273_4_Wohnungstueren`).
"""
from __future__ import annotations

import re
import unicodedata
from typing import NamedTuple

# Umlaute VOR dem Entfernen der Diakritika ersetzen — sonst wird aus „Planstände"
# „planstande" statt „planstaende" und derselbe Ordner hätte zwei Slots.
_UMLAUTE = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "æ": "ae", "œ": "oe"}

DEFAULT_ARCHIV_LABELS = frozenset({"archiv", "alt", "old", "ueberholt"})

# BKP-Code: 3 Ziffern (nicht mit 0 beginnend), optional Unterposition (`.4` / `_4`).
# Die Unterposition ist 1–2-stellig, sonst wäre `201_209` ein Code `201.20`.
_CODE = r"[1-9]\d{2}(?:[._]\d{1,2}(?!\d))?"
_CODE_ATOM_RE = re.compile(rf"({_CODE})(?![\d])")
_CODE_SEP_RE = re.compile(r"\s*\+\s*|[_\-]")
_PRAEFIX_ZAHL_RE = re.compile(r"(\d{1,3}(?:\.\d+)*[A-Za-z]?)(?=[\s_\-]|$)")
_PRAEFIX_BUCHSTABE_RE = re.compile(r"([A-Za-z])(?=[\s_\-])")
_TRENNER_RE = re.compile(r"^[0-9A-Za-z]{0,3}[\s]*[-–—_=~.*]{3,}\s*$")
_NICHT_WORT_RE = re.compile(r"[\W_]+")
_NOTIZ_TRENNER = " > "


class Zerlegung(NamedTuple):
    praefix: str
    label: str
    codes: list[str]


def pfad_schluessel(pfad) -> str:
    """Einzige Vergleichsform für Pfade: NFC, ohne abschliessenden Schrägstrich.

    macOS liefert Dateinamen als NFD, Datenbank und Konfiguration enthalten teils
    NFC — jeder Pfadvergleich im Ablage-Code läuft deshalb über diese Funktion.
    """
    s = unicodedata.normalize("NFC", str(pfad))
    return s.rstrip("/") if len(s) > 1 else s


def label_text(text: str) -> str:
    """Kleinschreibung, Umlaute ausgeschrieben, Rest nur Buchstaben/Ziffern mit Einzelleerzeichen."""
    s = unicodedata.normalize("NFC", text).lower()
    for k, v in _UMLAUTE.items():
        s = s.replace(k, v)
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return _NICHT_WORT_RE.sub(" ", s).strip()


def _codes_lesen(rest: str) -> tuple[list[str], str]:
    """Führende BKP-Codes ablösen. Gibt (Codes, Rest) zurück; ohne Code ([], rest)."""
    m = _CODE_ATOM_RE.match(rest)
    if not m:
        return [], rest
    atome = [m.group(1)]
    pos = m.end()
    # Gruppen: Atome mit `+` getrennt sind einzeln, mit `_`/`-` verbundene Paare ein Bereich.
    gruppen: list[list[str]] = [[m.group(1)]]
    while True:
        sep = _CODE_SEP_RE.match(rest, pos)
        if not sep:
            break
        nxt = _CODE_ATOM_RE.match(rest, sep.end())
        if not nxt:
            break
        plus = "+" in sep.group(0)
        if plus:
            gruppen.append([nxt.group(1)])
        else:
            gruppen[-1].append(nxt.group(1))
        atome.append(nxt.group(1))
        pos = nxt.end()
    codes: list[str] = []
    for g in gruppen:
        g = [c.replace("_", ".") for c in g]
        if len(g) == 2:
            codes.append(f"{g[0]}-{g[1]}")
        else:
            codes.extend(g)
    return codes, rest[pos:]


def zerlege(name: str) -> Zerlegung:
    """Ordnername → (Präfix, Label, Codes). Der Originalname bleibt beim Aufrufer."""
    s = unicodedata.normalize("NFC", name).strip()
    if _NOTIZ_TRENNER in s:
        s = s.split(_NOTIZ_TRENNER, 1)[0].strip()

    codes, rest = _codes_lesen(s)
    praefixe: list[str] = []
    if not codes:
        for _ in range(3):
            m = _PRAEFIX_ZAHL_RE.match(rest) or _PRAEFIX_BUCHSTABE_RE.match(rest)
            if not m:
                break
            danach = rest[m.end():].lstrip(" _-")
            if not danach:
                break  # ein Name, der NUR aus dem Präfix besteht, behält es als Label
            praefixe.append(m.group(1))
            rest = danach
            # Nach einem Präfix kann ein Code folgen (`1_221_Fenster` ist selten, aber möglich)
            c2, r2 = _codes_lesen(rest)
            if c2:
                codes, rest = c2, r2
                break

    label = label_text(rest)
    if not label:
        label = label_text(s)
        praefixe = []
        codes = []
    return Zerlegung("_".join(praefixe), label, codes)


def ist_trenner(name: str) -> bool:
    """`0-------`, `a ------`, `00-----`: Optische Trenner ohne eigenen Inhalt."""
    return bool(_TRENNER_RE.match(unicodedata.normalize("NFC", name).strip()))


def art_bestimmen(name: str, label: str | None = None, archiv_labels=None,
                  ausgeschlossen: bool = False) -> str:
    """`ausgeschlossen` kommt vom Aufrufer (walker._is_excluded_name), alles andere von hier."""
    if ausgeschlossen:
        return "ausgeschlossen"
    if ist_trenner(name):
        return "trenner"
    if label is None:
        label = zerlege(name).label
    archiv = DEFAULT_ARCHIV_LABELS if archiv_labels is None else {label_text(a) for a in archiv_labels}
    return "archiv" if label in archiv else "normal"


def label_pfad(rel_path: str) -> str:
    """`51_Ausfuehrung/51a_Planstände` → `ausfuehrung/planstaende` (Trenner übersprungen)."""
    teile = [t for t in pfad_schluessel(rel_path).split("/") if t and not ist_trenner(t)]
    return "/".join(zerlege(t).label for t in teile)
