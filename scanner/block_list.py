"""Manuelle Sperrliste für die MCP-Schnittstelle -- ergänzt die automatische
Norm-Erkennung (scanner/norms.py) um von Hand gesetzte Regeln.

Architekturprinzip identisch zu scanner/norms.py: redact_hits()/guard_read() sind
die EINZIGEN Stellen, die MCP-Antworten vor dem Verlassen des Prozesses sehen
(siehe web/api.py mcp_search/mcp_semantic_search/mcp_document), direkt neben den
entsprechenden Aufrufen aus scanner.norms -- ein zweiter, unabhängiger Gate-Layer,
kein Ersatz dafür.

Regeltypen:
- file:    Wert ist der SHA256-Hash (documents.hash) -- überlebt Verschiebungen,
           konsistent mit dem projektweiten Prinzip "Dateiidentität über Hash".
- pattern: Wert ist ein fnmatch-Glob gegen den Dateinamen (z.B. "*Lohn*").
- folder:  Wert ist entweder ein voller Ordnerpfad (Präfix-Match -- der Ordner
           selbst und alles darunter ist gesperrt) oder ein Wildcard-Muster wie
           "*Personal*" (matcht dann gegen jede einzelne Pfadkomponente, trifft
           also jeden so benannten Ordner unabhängig vom genauen NAS-Pfad).
- project: Wert ist eine project_id (als Text) -- das ganze Projekt ist gesperrt.
"""
from __future__ import annotations

import fnmatch
import logging
import sqlite3
from pathlib import Path

from scanner.pathutil import is_under

log = logging.getLogger(__name__)

BLOCKED_NOTICE = "🔒 Gesperrt — Inhalt nicht über MCP verfügbar (Sperrliste)"


# ── Regeln laden (Singleton-Cache, wie scanner.norms._classifier) ──────────────

_rules: list[dict] | None = None


def load_rules(conn: sqlite3.Connection) -> list[dict]:
    global _rules
    if _rules is None:
        reload_block_rules(conn)
    return _rules


def reload_block_rules(conn: sqlite3.Connection) -> list[dict]:
    global _rules
    _rules = [dict(r) for r in conn.execute(
        "SELECT * FROM block_rules WHERE enabled = 1"
    ).fetchall()]
    return _rules


# ── Matching ────────────────────────────────────────────────────────────────────

def _matching_rule(conn: sqlite3.Connection, doc_hash: str | None, filename: str | None,
                    path: str | None, project_id: int | None) -> dict | None:
    for rule in load_rules(conn):
        t, v = rule["type"], rule["value"]
        if t == "file" and doc_hash and v == doc_hash:
            return rule
        elif t == "pattern" and (
            (filename and fnmatch.fnmatch(filename.lower(), v.lower())) or
            (path and fnmatch.fnmatch(path.lower(), v.lower()))
        ):
            # Gegen Dateiname UND vollen Pfad geprüft -- "*Personal*" trifft so sowohl
            # eine Datei "Personal_Muster.pdf" als auch jede Datei in einem Ordner, der
            # "Personal" im Namen trägt, ohne dass der Nutzer den exakten NAS-Pfad
            # kennen muss (portabel, siehe die 7 vorbelegten Vorschläge in Settings).
            return rule
        elif t == "folder" and path and (
            # Enthält v ein '*': gegen jede Pfadkomponente (den einzelnen Ordnernamen)
            # geglobbt, exakte Namensgleichheit ansonsten -- gleiche Wildcard-Syntax wie
            # der "pattern"-Typ und wie scanner.excluded_folders, aber ordnerweise statt
            # gegen den ganzen Pfad, damit "*privat*" jeden so benannten Ordner in
            # beliebiger Tiefe trifft statt nur Pfade, deren GESAMTER String matcht.
            # Ohne '*': Präfix-Match wie bisher (v selbst UND alles darunter gesperrt).
            any(fnmatch.fnmatch(part.lower(), v.lower()) for part in Path(path).parts)
            if "*" in v else is_under(path, v)
        ):
            return rule
        elif t == "project" and project_id is not None and str(project_id) == v:
            return rule
    return None


def is_blocked(conn: sqlite3.Connection, doc_id: int | None, path: str | None,
               project_id: int | None = None) -> tuple[bool, str | None]:
    """Zwei unabhängige Nachschlagewege wie is_norm_doc(): direkt per doc_id (holt
    Hash/Dateiname/Projekt selbst nach) oder mit bereits bekannten Werten. Fail-
    closed: ein Fehler beim Nachschlagen sperrt, statt stillschweigend durchzulassen."""
    try:
        doc_hash = filename = None
        if doc_id is not None:
            row = conn.execute(
                "SELECT hash, filename, project_id FROM documents WHERE id = ?", (doc_id,)
            ).fetchone()
            if row:
                doc_hash, filename, project_id = row["hash"], row["filename"], row["project_id"]
        elif path:
            filename = path.rsplit("/", 1)[-1]
        rule = _matching_rule(conn, doc_hash, filename, path, project_id)
        if rule:
            label = rule.get("label") or rule["value"]
            return True, f"Sperrliste: {label}"
        return False, None
    except Exception:
        return True, "Sperrliste: Fehler bei Prüfung"


def redact_hits(conn: sqlite3.Connection, hits: list[dict]) -> list[dict]:
    """Wie scanner.norms.redact_hits() -- Redaktion auf Record-Ebene. Läuft NACH
    der Norm-Redaktion; Treffer, die dort schon als is_norm markiert wurden,
    werden hier übersprungen (schon redigiert, kein zweiter Grund nötig)."""
    for h in hits:
        if h.get("is_norm"):
            continue
        doc_id = h.get("id", h.get("document_id"))
        path = h.get("path", h.get("filepath"))
        blocked, reason = is_blocked(conn, doc_id, path)
        if blocked:
            h["is_blocked"] = True
            h["block_reason"] = reason
            if "excerpt" in h:
                h["excerpt"] = BLOCKED_NOTICE
            if "content" in h:
                h["content"] = BLOCKED_NOTICE
            h.pop("text", None)
            h.pop("page_text", None)
    return hits


def guard_read(conn: sqlite3.Connection, doc_id: int, filename: str,
               path: str | None) -> str | None:
    """Wie scanner.norms.guard_read() -- gibt einen Verweigerungstext zurück wenn
    doc_id gesperrt ist, sonst None."""
    blocked, reason = is_blocked(conn, doc_id, path)
    if not blocked:
        return None
    return (
        f"🔒 **{filename}** (ID {doc_id}) — {reason}.\n"
        f"Der Inhalt wird nicht über die MCP-Schnittstelle ausgegeben.\n\n"
        f"Pfad: `{path}`\n"
        f"Lokal öffnen: `open_file({doc_id})` · "
        f"Im Finder zeigen: `reveal_file({doc_id})` · "
        f"Volltextsuche direkt in Archivio (offline, dort uneingeschränkt)"
    )


def assert_no_blocked_text(payload: list[dict]) -> None:
    """Letzte Prüfung vor der Serialisierung, analog assert_no_norm_text()."""
    for h in payload:
        if not h.get("is_blocked"):
            continue
        for field in ("excerpt", "content", "text", "page_text"):
            if h.get(field) not in (None, "", BLOCKED_NOTICE):
                raise RuntimeError(
                    f"Sperrlisten-Text-Leck bei Dokument {h.get('id', h.get('document_id'))} (Feld {field})"
                )


# ── Live-Zähler für die Settings-Seite ──────────────────────────────────────────

def matching_count(conn: sqlite3.Connection, rule_type: str, value: str) -> int:
    """Wie viele aktuell indexierte Dokumente eine Regel gerade erfasst -- rein
    informativ in der Sperrlisten-Verwaltung, kein Teil des Gates selbst."""
    if rule_type == "file":
        return conn.execute(
            "SELECT COUNT(*) FROM documents WHERE hash = ?", (value,)
        ).fetchone()[0]
    if rule_type == "project":
        try:
            pid = int(value)
        except ValueError:
            return 0
        return conn.execute(
            "SELECT COUNT(*) FROM documents WHERE project_id = ?", (pid,)
        ).fetchone()[0]
    if rule_type == "pattern":
        rows = conn.execute("""
            SELECT d.filename, dp.path FROM documents d
            LEFT JOIN document_paths dp ON dp.document_id = d.id AND dp.is_primary = 1
        """).fetchall()
        v = value.lower()
        return sum(
            1 for r in rows
            if fnmatch.fnmatch(r["filename"].lower(), v)
            or (r["path"] and fnmatch.fnmatch(r["path"].lower(), v))
        )
    if rule_type == "folder":
        rows = conn.execute("SELECT DISTINCT path FROM document_paths").fetchall()
        if "*" in value:
            v = value.lower()
            return sum(
                1 for r in rows
                if any(fnmatch.fnmatch(part.lower(), v) for part in Path(r["path"]).parts)
            )
        return sum(1 for r in rows if is_under(r["path"], value))
    return 0
