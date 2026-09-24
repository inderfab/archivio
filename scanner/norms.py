"""Norm-Erkennung und MCP-Sperre für technische Normen (SIA/VSS/EN/DIN/ISO).

Rechtlicher Rahmen: SIA- und VSS-Normen sind privatrechtliche Werke mit Büro-/
Einzelplatzlizenz. Weitergabe an Dritte (ein Cloud-LLM ist ein Dritter) ist
lizenzwidrig -- auch für Normen, die per Verweis verbindlich erklärt wurden.
Gesetze/Verordnungen/Entscheide sind nach URG Art. 5 gemeinfrei und werden über
die notwendige Bedingung (Herausgeber-/Lizenzmarker) NICHT mitgesperrt.
Metadaten (Normnummer, Titel, Pfad, Seitenzahl) sind Fakten, keine geschützte
Werkform, und dürfen ausgegeben werden. Die lokale OCR-Kopie im Index ist
zulässiger Eigengebrauch der lizenzierten Norm.

Architekturprinzip (analog is_path_allowed()): Prüfung pro Tool/Route IST die
Fehlerklasse -- ein Aufrufer wird vergessen und die Sperre ist offen. redact_hits()/
guard_read() sind deshalb die EINZIGEN Stellen, die MCP-Antworten vor dem
Verlassen des Prozesses sehen (siehe web/api.py mcp_search/mcp_document) -- ein zentrales Gate statt Prüfung in jeder Route einzeln.

Portabilität (Ziel: funktioniert bei einem fremden Büro ohne Konfiguration):
config/norms.yaml enthält nur portable Regeln (Herausgeber-/Lizenzmarker,
Normnummer-Patterns) -- keine Pfade. Bürospezifische Norm-Ordner werden gelernt
(norm_folders-Tabelle, siehe learn_norm_folders()) und vom Nutzer bestätigt,
nicht konfiguriert.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml

from scanner.pathutil import is_under as _is_under
from scanner.pathutil import norm_path as _norm_path

log = logging.getLogger(__name__)

NORM_NOTICE = "🔒 Norm — Inhalt gesperrt (Urheber-/Lizenzrecht)"

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "norms.yaml"


@dataclass(frozen=True)
class NormVerdict:
    is_norm: bool
    reason: str | None      # "folder:confirmed" | "publisher+norm_number" | ...
    score: int


def load_config(path: Path | None = None) -> dict:
    """Lädt config/norms.yaml. Die Datei wird unverändert mit der App ausgeliefert
    (kein Nutzer-Datenverzeichnis wie config.yaml) -- Pfad relativ zum Package."""
    p = path or _CONFIG_PATH
    try:
        with open(p, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.warning("config/norms.yaml nicht gefunden (%s) — Norm-Erkennung deaktiviert", p)
        return {"enabled": False}


class NormClassifier:
    def __init__(self, cfg: dict, confirmed_folders: list[str] | None = None):
        self.cfg = cfg
        self.enabled = cfg.get("enabled", True)
        self.publisher = cfg.get("publisher_markers", [])
        self.license = cfg.get("license_markers", [])
        self.weak = cfg.get("weak_markers", [])
        self.num_res = [re.compile(p) for p in cfg.get("norm_number_patterns", [])]
        self.fn_res = [re.compile(p) for p in cfg.get("filename_patterns", [])]
        # Aus norm_folders geladen, NICHT aus der YAML. Beim Start und nach
        # jeder Bestätigung in der UI neu einlesen (siehe confirmed_norm_folders()).
        self.folders = [_norm_path(p) for p in (confirmed_folders or [])]

    # --- Layer 2: gelernte Ordner, ohne DB-Lookup auf documents --------------
    def folder_is_norm(self, path: str) -> bool:
        if not self.enabled or not path:
            return False
        return any(_is_under(path, root) for root in self.folders)

    # --- Vollklassifikation für Scan/Ingest ---------------------------------
    def classify(self, path: str, text: str | None) -> NormVerdict:
        if not self.enabled:
            return NormVerdict(False, None, 0)

        # Bestätigter Norm-Ordner schlägt alles. Fängt auch Scans ohne Textlayer,
        # bei denen die Inhaltsprüfung nichts findet.
        if self.folder_is_norm(path):
            return NormVerdict(True, "folder:confirmed", 99)

        s = self.cfg["scores"]
        score, reasons = 0, []
        head = (text or "")[: self.cfg.get("content_scan_chars", 4000)]
        low = head.casefold()

        has_publisher = any(m.casefold() in low for m in self.publisher)
        has_license = any(m.casefold() in low for m in self.license)

        # NOTWENDIGE BEDINGUNG. Ohne Herausgeber- oder Lizenzvermerk niemals Norm,
        # egal wie viele andere Signale feuern. Schützt gemeinfreie Gesetze/
        # Verordnungen (URG Art. 5), die keinen SIA-/VSS-Herausgebervermerk tragen.
        if not (has_publisher or has_license):
            return NormVerdict(False, None, 0)

        if has_license:
            score += s["license_marker"];   reasons.append("license")
        if has_publisher:
            score += s["publisher_marker"]; reasons.append("publisher")
        if any(r.search(head) for r in self.num_res):
            score += s["norm_number_text"]; reasons.append("norm_number")
        if any(r.search(os.path.basename(path)) for r in self.fn_res):
            score += s["filename"];         reasons.append("filename")

        weak_hits = sum(1 for m in self.weak if m.casefold() in low)
        if weak_hits:
            score += min(weak_hits * s["weak_marker"], s["weak_marker_max"])
            reasons.append("weak")

        is_norm = score >= self.cfg.get("threshold", 4)
        return NormVerdict(is_norm, "+".join(reasons) or None, score)


# ── Singleton-Zugriff ──────────────────────────────────────────────────────────
# EIN Classifier-Objekt pro Prozess, mit den bestätigten Ordnern aus der DB.
# reload_classifier() nach jeder Bestätigung/Ablehnung in der UI aufrufen (web/
# dashboard.py), sonst wirkt eine Bestätigung erst nach Neustart des Servers.

_classifier: NormClassifier | None = None


def confirmed_norm_folders(conn: sqlite3.Connection) -> list[str]:
    return [r["path"] for r in conn.execute(
        "SELECT path FROM norm_folders WHERE status = 'confirmed'"
    ).fetchall()]


def get_classifier(conn: sqlite3.Connection) -> NormClassifier:
    global _classifier
    if _classifier is None:
        reload_classifier(conn)
    return _classifier


def reload_classifier(conn: sqlite3.Connection) -> NormClassifier:
    global _classifier
    _classifier = NormClassifier(load_config(), confirmed_norm_folders(conn))
    return _classifier


def looks_like_norm_query(conn: sqlite3.Connection, query: str) -> str | None:
    """Prüft, ob eine MCP-Suchanfrage selbst wie eine Normnummer aussieht (SIA 400,
    EN 1090, ...) -- unabhängig davon, ob überhaupt ein Treffer gefunden wird. Ohne
    das bekommt Claude bei einer Norm, die gar nicht (mehr) im Index liegt oder
    ausserhalb der freigegebenen Projekte, schlicht "keine Treffer" und muss sich
    die Urheberrechtslage selbst zusammenreimen -- mit dem Risiko, das falsch oder
    unpräzise zu tun. Gibt die erkannte Normnummer zurück (fürs Protokoll/die
    Nutzermeldung), sonst None."""
    if not query:
        return None
    try:
        classifier = get_classifier(conn)
    except Exception:
        return None
    if not classifier.enabled:
        return None
    for pattern in classifier.num_res:
        m = pattern.search(query)
        if m:
            return m.group(0)
    return None


_GERMAN_MONTHS = {
    "januar": "01", "februar": "02", "märz": "03", "maerz": "03", "april": "04",
    "mai": "05", "juni": "06", "juli": "07", "august": "08", "september": "09",
    "oktober": "10", "november": "11", "dezember": "12",
}

_VALID_FROM_PATTERNS = [
    # "Gültig ab: 2018-04-01" -- moderne SIA-Normen, exaktes Datum. Häufigstes und
    # präzisestes Muster, deshalb zuerst versucht.
    (re.compile(r"G[üu]ltig ab\D{0,5}(\d{4})-(\d{2})-(\d{2})", re.IGNORECASE), "ymd"),
    # "Es tritt am 1. Juli 2000 in Kraft" -- steht bei Merkblättern/älteren Normen
    # oft erst im Schlussabschnitt "Genehmigung und Inkrafttreten" am ENDE des
    # Dokuments, nicht auf der Titelseite (siehe extract_valid_from -- wird deshalb
    # auch gegen das Textende geprüft). Genauso exakt wie "Gültig ab", daher
    # gleiche Priorität.
    (re.compile(
        r"tritt\s+am\s+(\d{1,2})\.\s*(" + "|".join(_GERMAN_MONTHS) + r")\s+(\d{4})\s+in\s+Kraft",
        re.IGNORECASE,
    ), "dmonthy"),
    # "SIA 400:2000" / "SIA 251:2008" -- Jahr direkt in der Bezeichnung, bei älteren
    # Normen ohne "Gültig ab"-Vermerk der zuverlässigste verbleibende Anhaltspunkt.
    (re.compile(r"\bSIA\s*[\d./]+\s*:\s*(\d{4})\b"), "y"),
    # "Copyright © 2000 by SIA" -- OCR liest das oft fehlerhaft ein ("Copvright"
    # statt "Copyright", "@" statt "©", siehe z.B. SIA 2017 im OCR-Ordner) --
    # Muster entsprechend tolerant. Letzter Fallback, etwas ungenauer (Copyright-
    # Jahr kann vom eigentlichen Ausgabejahr abweichen), aber besser als gar kein
    # Datum.
    (re.compile(r"Cop[yv]right\s*[©@]?\s*(\d{4})\s*by\s*SIA", re.IGNORECASE), "y"),
]


def extract_valid_from(text: str | None) -> str | None:
    """Sucht im (bereits extrahierten) Dokumenttext nach dem Gültigkeitsdatum einer
    Norm -- Prioritätenkette von genau (Tagesdatum) zu ungenau (nur Jahr), siehe
    _VALID_FROM_PATTERNS. Durchsucht Anfang UND Ende des Dokuments (je die ersten/
    letzten paar tausend Zeichen), nicht nur die Titelseite -- bei Merkblättern
    steht "Genehmigung und Inkrafttreten" oft erst im Schlussabschnitt, nicht
    vorne. Der ganze Fliesstext dazwischen wird bewusst NICHT durchsucht, damit
    ein zufälliges Datum mitten im Dokument nicht fälschlich als Gültigkeitsdatum
    genommen wird. Gibt IMMER ein volles ISO-Datum zurück (Jahr-only-Treffer
    werden auf den 1. Januar gelegt) oder None, wenn kein Muster passt -- das ist
    der erwartete, häufige Fall bei älteren/untypisch formatierten Normen (siehe
    Docstring-Kommentar in _classify_norm). Fenstergrösse 6000 Zeichen (nicht nur
    4000) -- bei umfangreicheren Normen mit mehrsprachigem oder längerem Vorspann
    vor der eigentlichen Titelseite (z.B. SIA-Ordnungen, die dem Titel oft eine
    französische/italienische Fassung voranstellen) reichten 4000 Zeichen nicht
    immer bis zum "Gültig ab"-Vermerk."""
    if not text:
        return None
    WINDOW = 6000
    zones = [text[:WINDOW]]
    if len(text) > WINDOW:
        zones.append(text[-WINDOW:])
    for pattern, kind in _VALID_FROM_PATTERNS:
        for zone in zones:
            m = pattern.search(zone)
            if not m:
                continue
            groups = m.groups()
            if kind == "ymd":
                year, month, day = groups
            elif kind == "dmonthy":
                day, month_name, year = groups
                month = _GERMAN_MONTHS[month_name.lower()]
                day = day.zfill(2)
            else:  # "y"
                year, month, day = groups[0], "01", "01"
            try:
                from datetime import date
                date(int(year), int(month), int(day))  # wirft bei unsinnigem Datum
            except ValueError:
                continue
            return f"{year}-{month}-{day}"
    return None


def find_norm_locations(conn: sqlite3.Connection, norm_ref: str) -> list[dict]:
    """Findet lokal vorhandene Normen anhand ihrer Nummer -- BEWUSST unabhängig von
    der MCP-Projekt-Freigabe (projects.mcp_enabled). Pfad/Dateiname sind Fakten,
    keine geschützte Werkform (siehe Modul-Docstring oben), und dürfen deshalb auch
    für ein nicht freigegebenes Projekt genannt werden -- nur der Norminhalt selbst
    bleibt in jedem Fall gesperrt (dafür sorgen weiterhin redact_hits()/guard_read(),
    diese Funktion liefert nie Inhalt, nur Metadaten). Ohne das würde eine Norm in
    einem nicht freigegebenen Projekt beim Suchen einfach so wirken, als gäbe es sie
    gar nicht -- statt "liegt hier, aber Inhalt gesperrt".

    Matcht nur auf der ZAHL, an einer Ziffern-Grenze (kein Teil einer längeren
    Zahl), und nur im DATEINAMEN -- nicht mehr auf Buchstaben+Zahl zusammen
    irgendwo im ganzen Pfad. Reale Normdateien heissen oft schlicht "400.pdf" ohne
    "SIA" im Namen (der Herausgeber steht nur im übergeordneten Ordnernamen, der
    von echtem Fliesstext wie "...Normen_SIA/S I A NORMEN/..." durchsetzt ist) --
    ein Abgleich auf den gesamten Pfad verlangte "sia" und "400" direkt
    nebeneinander und fand dadurch selbst die offiziell abgelegten Normen nicht."""
    m = re.match(r"\s*[a-zA-Z]*[\s_./-]*0*(\d+)", norm_ref or "")
    if not m:
        return []
    digits = m.group(1)
    num_re = re.compile(r"(?<!\d)0*" + re.escape(digits) + r"(?!\d)")
    rows = conn.execute("""
        SELECT d.filename, p.name AS project_name, dp.path,
               d.norm_valid_from AS valid_from, d.norm_check_status AS check_status
        FROM documents d
        LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        LEFT JOIN projects p ON p.id = d.project_id
        WHERE d.is_norm = 1
    """).fetchall()
    matches = []
    for r in rows:
        if num_re.search((r["filename"] or "").lower()):
            matches.append({
                "path": r["path"], "filename": r["filename"], "project": r["project_name"],
                "valid_from": r["valid_from"], "check_status": r["check_status"],
            })
    return matches


# ── MCP-Gate ────────────────────────────────────────────────────────────────────

def is_norm_doc(conn: sqlite3.Connection, doc_id: int | None, path: str | None) -> bool:
    """Zwei unabhängige Schichten, fail-closed verknüpft."""
    try:
        classifier = get_classifier(conn)
        # L2: bestätigter Norm-Ordner, unabhängig vom Indexzustand.
        # Deckt frisch abgelegte, noch nicht gescannte Normen ab.
        if path and classifier.folder_is_norm(path):
            return True
        # L1: DB-Flag aus dem Scan
        if doc_id is not None:
            row = conn.execute(
                "SELECT is_norm FROM documents WHERE id = ?", (doc_id,)
            ).fetchone()
            if row and row["is_norm"]:
                return True
        return False
    except Exception:
        return True   # fail-closed: im Zweifel sperren


def redact_hits(conn: sqlite3.Connection, hits: list[dict]) -> list[dict]:
    """Redaktion auf Record-Ebene, VOR dem Formatieren der Trefferzeile -- nicht per
    Regex auf dem fertigen Ausgabestring. Erwartet Keys 'id'/'document_id' und
    'path'/'filepath' (beide Varianten kommen in diesem Codebase vor)."""
    for h in hits:
        doc_id = h.get("id", h.get("document_id"))
        path = h.get("path", h.get("filepath"))
        if is_norm_doc(conn, doc_id, path):
            h["is_norm"] = True
            if "excerpt" in h:
                h["excerpt"] = NORM_NOTICE
            if "content" in h:
                h["content"] = NORM_NOTICE
            h.pop("text", None)
            h.pop("page_text", None)
            # Gültigkeitsdatum/Status sind reine Metadaten (keine geschützte
            # Werkform, siehe Modul-Docstring) -- dürfen deshalb auch bei
            # gesperrtem Inhalt mitgegeben werden.
            if doc_id is not None:
                row = conn.execute(
                    "SELECT norm_valid_from, norm_check_status FROM documents WHERE id = ?",
                    (doc_id,),
                ).fetchone()
                if row:
                    h["norm_valid_from"] = row["norm_valid_from"]
                    h["norm_check_status"] = row["norm_check_status"]
    return hits


def guard_read(conn: sqlite3.Connection, doc_id: int, filename: str, path: str | None) -> str | None:
    """Gibt einen Verweigerungstext zurück wenn doc_id eine Norm ist, sonst None
    (aufrufende Route liefert dann normal den Inhalt aus). Der Hinweis auf
    open_file/reveal_file ist funktional wichtig, nicht Höflichkeit: er sagt dem
    Client, was STATTDESSEN möglich ist -- fehlt er, probiert das Modell dieselbe
    Sperre über andere Tools erneut zu umgehen."""
    if not is_norm_doc(conn, doc_id, path):
        return None
    meta_lines = ""
    row = conn.execute(
        "SELECT norm_valid_from, norm_check_status FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()
    if row and row["norm_valid_from"]:
        meta_lines += f"Gültig ab: {row['norm_valid_from']}\n"
    if row and row["norm_check_status"] and row["norm_check_status"] != "ungeprüft":
        label = {"aktuell": "Aktuell", "veraltet": "Veraltet ⚠️"}.get(
            row["norm_check_status"], row["norm_check_status"]
        )
        meta_lines += f"Aktualität: {label}\n"
    return (
        f"🔒 **{filename}** (ID {doc_id}) — als Norm klassifiziert.\n"
        f"Der Inhalt wird aus urheber- und lizenzrechtlichen Gründen nicht über die "
        f"MCP-Schnittstelle ausgegeben. Normtexte dürfen nicht an externe KI-Dienste "
        f"übermittelt werden.\n\n"
        f"Pfad: `{path}`\n"
        f"{meta_lines}"
        f"Lokal öffnen: `open_file({doc_id})` · "
        f"Im Finder zeigen: `reveal_file({doc_id})` · "
        f"Volltextsuche direkt in Archivio (offline, dort uneingeschränkt)"
    )


_TYPE_RE = re.compile(r"(?i)\b(VSS|SIA|SN|EN|DIN|ISO|IEC)\b")


def guess_norm_type(filename: str, text: str | None) -> str:
    """Rein kosmetischer Anzeige-Wert für die Normen-Liste (/norms) -- KEIN Teil der
    Klassifikation. Dateiname zuerst (schnell, oft aussagekräftig), sonst die ersten
    6000 Zeichen des Volltexts (deckt Fälle wie 'SIA 180.082.pdf' ab, deren
    Herausgeber-Nummer nur im Dokument selbst steht, nicht im Dateinamen). Mindestens
    so gross wie content_scan_chars der Klassifikation (config/norms.yaml, Default
    4000) -- sonst kann ein Dokument als Norm erkannt werden (Herausgebername wie
    "Schweizerischer Ingenieur- und Architektenverein" enthält kein isoliertes
    "SIA"-Kürzel), dessen kurzes Kürzel aber ausserhalb eines kleineren Fensters
    liegt (z.B. bei mehrsprachigem Vorspann vor der eigentlichen Titelseite) und
    dann fälschlich als generisches "Norm" statt "SIA" angezeigt wird."""
    for source in (filename or "", (text or "")[:6000]):
        m = _TYPE_RE.search(source)
        if m:
            return m.group(1).upper()
    return "Norm"


_NUMBER_RE = re.compile(r"(\d{2,6}(?:[.\-/]\d{1,4}){0,2})")


def guess_norm_number(filename: str, text: str | None) -> str | None:
    """Extrahiert die reine Normnummer (z.B. "400", "180.081", "640050") aus
    Dateiname oder Text -- fürs Zusammensetzen der Shop-URL beim manuellen
    Aktualitäts-Abgleich (siehe scanner/norm_freshness.py). Rein heuristisch wie
    guess_norm_type(), keine Garantie auf Treffer bei unüblichen Dateinamen --
    das ist unkritisch, der Abgleich fällt in dem Fall auf "ungeprüft" zurück."""
    stem = Path(filename or "").stem
    m = _NUMBER_RE.search(stem)
    if m:
        return m.group(1)
    m = _NUMBER_RE.search((text or "")[:2000])
    if m:
        return m.group(1)
    return None


def assert_no_norm_text(payload: list[dict]) -> None:
    """Letzte Prüfung vor der Serialisierung -- kostet nichts und fängt jedes
    künftige Tool/jede künftige Route, die redact_hits() vergisst."""
    for h in payload:
        if not h.get("is_norm"):
            continue
        for field in ("excerpt", "content", "text", "page_text"):
            if h.get(field) not in (None, "", NORM_NOTICE):
                raise RuntimeError(
                    f"Norm-Text-Leck bei Dokument {h.get('id', h.get('document_id'))} (Feld {field})"
                )
