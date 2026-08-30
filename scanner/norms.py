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
Verlassen des Prozesses sehen (siehe web/api.py mcp_search/mcp_semantic_search/
mcp_document) -- ein zentrales Gate statt Prüfung in jeder Route einzeln.

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
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

NORM_NOTICE = "🔒 Norm — Inhalt gesperrt (Urheber-/Lizenzrecht)"

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "norms.yaml"


def _norm_path(p: str) -> str:
    """macOS/SMB liefert NFD-zerlegte Umlaute, DB/YAML enthalten NFC.
    Ohne Normalisierung matcht 'Behörden' nicht gegen 'Behörden'."""
    return unicodedata.normalize("NFC", os.path.normpath(p))


def _is_under(path: str, root: str) -> bool:
    """Präfix-Match auf Pfadkomponenten, nicht auf Strings.
    Verhindert, dass '/x/Normen2' gegen '/x/Normen' matcht."""
    path, root = _norm_path(path), _norm_path(root)
    return path == root or path.startswith(root + os.sep)


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
    'path'/'filepath' (beide Varianten kommen in diesem Codebase vor, siehe
    web/api.py::mcp_search vs. mcp_semantic_search)."""
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
    return hits


def guard_read(conn: sqlite3.Connection, doc_id: int, filename: str, path: str | None) -> str | None:
    """Gibt einen Verweigerungstext zurück wenn doc_id eine Norm ist, sonst None
    (aufrufende Route liefert dann normal den Inhalt aus). Der Hinweis auf
    open_file/reveal_file ist funktional wichtig, nicht Höflichkeit: er sagt dem
    Client, was STATTDESSEN möglich ist -- fehlt er, probiert das Modell dieselbe
    Sperre über andere Tools erneut zu umgehen."""
    if not is_norm_doc(conn, doc_id, path):
        return None
    return (
        f"🔒 **{filename}** (ID {doc_id}) — als Norm klassifiziert.\n"
        f"Der Inhalt wird aus urheber- und lizenzrechtlichen Gründen nicht über die "
        f"MCP-Schnittstelle ausgegeben. Normtexte dürfen nicht an externe KI-Dienste "
        f"übermittelt werden.\n\n"
        f"Pfad: `{path}`\n"
        f"Lokal öffnen: `open_file({doc_id})` · "
        f"Im Finder zeigen: `reveal_file({doc_id})` · "
        f"Volltextsuche direkt in Archivio (offline, dort uneingeschränkt)"
    )


_TYPE_RE = re.compile(r"(?i)\b(VSS|SIA|SN|EN|DIN|ISO|IEC)\b")


def guess_norm_type(filename: str, text: str | None) -> str:
    """Rein kosmetischer Anzeige-Wert für die Normen-Liste (/norms) -- KEIN Teil der
    Klassifikation. Dateiname zuerst (schnell, oft aussagekräftig), sonst die ersten
    2000 Zeichen des Volltexts (deckt Faelle wie 'SIA 180.082.pdf' ab, deren
    Herausgeber-Nummer nur im Dokument selbst steht, nicht im Dateinamen)."""
    for source in (filename or "", (text or "")[:2000]):
        m = _TYPE_RE.search(source)
        if m:
            return m.group(1).upper()
    return "Norm"


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
