"""CSV-Exporte der vier Verlaeufe (Suche, Scan, Dokumentenverarbeitung, MCP).

Zwei Modi, ueberall gleich:
  klartext  Projektname, Dateiname/-pfad und Suchanfragen im Klartext.
  anonym    Projektname -> "Projekt <Nr>", Dateipfad/-name nur als Endung, Suchanfragen nur
            als Kennwerte (Wortanzahl, Laenge). Standard, wenn nichts angegeben ist.

Geraete- oder Personenbezug gibt es in den Protokollen nicht."""
from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path

# Pro Scan-Zeile im Klartext nur so viele Fehlerdateien: dieselben defekten Dateien stehen
# sonst in jeder Zeile jeder Nacht (ein Feld erreichte ueber 500'000 Zeichen).
MAX_FEHLERDATEIEN = 20

KLARTEXT = "klartext"
ANONYM = "anonym"

BESCHREIBUNG = {
    KLARTEXT: "Projektname, Dateipfad und Suchanfragen werden in Klartext exportiert.",
    ANONYM: ("Projektname wird durch Nummer ersetzt, der Dateipfad wird nicht exportiert, nur "
             "die Endung (Dateiformat), und bei der Suche werden nur Kennwerte (Wortanzahl, "
             "Länge, Trefferanzahl, Dauer und Klicks) exportiert."),
}


def norm_mode(modus: str | None) -> str:
    return KLARTEXT if (modus or "").lower() == KLARTEXT else ANONYM


def _csv(header: list[str], rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def _proj(mode: str, project_id, names: dict) -> str:
    if mode == KLARTEXT:
        return names.get(project_id, "") or ""
    return f"Projekt {project_id}" if project_id else ""


def _ext(name_or_path: str | None) -> str:
    return Path(name_or_path or "").suffix.lower()


def _words(query: str | None) -> int:
    return len((query or "").split())


def _project_names(conn) -> dict:
    return {p["id"]: p["name"] for p in conn.execute("SELECT id, name FROM projects")}


def search_csv(conn, mode: str) -> str:
    rows = conn.execute("SELECT * FROM search_log ORDER BY ts DESC LIMIT 20000").fetchall()
    names = _project_names(conn)
    if mode == KLARTEXT:
        return _csv(
            ["Zeitstempel", "Art", "Anfrage", "Projekt", "Filter", "Treffer", "Dauer (ms)", "Klicks"],
            ([r["ts"], "Suche", r["query"] or "", _proj(mode, r["project_id"], names),
              r["filters"] or "", r["result_count"], r["duration_ms"], r["clicks"]] for r in rows))
    return _csv(
        ["Zeitstempel", "Art", "Wortanzahl", "Länge (Zeichen)", "Projekt", "Filter",
         "Treffer", "Dauer (ms)", "Klicks"],
        ([r["ts"], "Suche", _words(r["query"]), len(r["query"] or ""),
          _proj(mode, r["project_id"], names), r["filters"] or "",
          r["result_count"], r["duration_ms"], r["clicks"]] for r in rows))


def _reason_category(reason: str | None) -> str:
    r = (reason or "").lower()
    if "norm" in r:
        return "Norm erkannt"
    if "sperr" in r:
        return "Sperrliste"
    if "freigegeben" in r:
        return "Nicht für Claude freigegeben"
    return "Sonstiger Grund" if r else ""


def mcp_csv(conn, mode: str, clause: str = "", params: list | None = None) -> str:
    rows = conn.execute(
        f"SELECT * FROM mcp_log {clause} ORDER BY ts DESC LIMIT 5000", params or []
    ).fetchall()
    names = _project_names(conn)
    out = []
    for r in rows:
        files = json.loads(r["files_json"] or "[]")
        blocked = json.loads(r["blocked_json"] or "[]")
        if mode == KLARTEXT:
            out.append([
                r["ts"], r["tool"], r["query"] or "", _proj(mode, r["project_id"], names), r["status"],
                "; ".join(f.get("filename") or f.get("path") or "" for f in files),
                r["chars_sent"],
                "; ".join(
                    f"{b.get('filename') or b.get('path')} ({b.get('reason')})"
                    if (b.get("filename") or b.get("path")) else (b.get("reason") or "")
                    for b in blocked),
            ])
        else:
            exts = sorted({_ext(f.get("filename") or f.get("path")) for f in files} - {""})
            cats = sorted({_reason_category(b.get("reason")) for b in blocked} - {""})
            out.append([
                r["ts"], r["tool"], _words(r["query"]), len(r["query"] or ""),
                _proj(mode, r["project_id"], names), r["status"],
                len(files), "; ".join(exts), r["chars_sent"], len(blocked), "; ".join(cats),
            ])
    if mode == KLARTEXT:
        return _csv(["Zeitstempel", "Tool", "Anfrage", "Projekt", "Status", "Übermittelte Dateien",
                     "Zeichen", "Blockiert (Grund)"], out)
    return _csv(["Zeitstempel", "Tool", "Wortanzahl", "Länge (Zeichen)", "Projekt", "Status",
                 "Anzahl Dateien", "Dateiformate", "Zeichen", "Anzahl blockiert", "Blockiert (Art)"], out)


def scan_csv(conn, mode: str) -> str:
    rows = conn.execute("SELECT * FROM scan_log ORDER BY started_at DESC LIMIT 5000").fetchall()
    out = []
    for r in rows:
        errors = json.loads(r["errors_json"] or "[]")
        if mode == KLARTEXT:
            shown = [e.get("path") or e.get("filename") or "" for e in errors[:MAX_FEHLERDATEIEN]]
            err = "; ".join(shown)
            if len(errors) > MAX_FEHLERDATEIEN:
                err += f"; … und {len(errors) - MAX_FEHLERDATEIEN} weitere"
            proj = r["project_name"] or ""
        else:
            err = "; ".join(sorted({_ext(e.get("filename") or e.get("path")) for e in errors} - {""}))
            proj = _proj(mode, r["project_id"], {})
        out.append([
            r["started_at"], r["finished_at"] or "", r["duration_s"] if r["duration_s"] is not None else "",
            r["status"], proj, r["total"], r["processed"], r["new_count"], r["skipped"],
            r["error_count"], r["peak_memory_mb"], r["peak_cpu_pct"], r["batch_id"] or "", err,
        ])
    return _csv(
        ["Start", "Ende", "Dauer (s)", "Status", "Projekt", "Dateien gesamt", "Verarbeitet", "Neu",
         "Übersprungen", "Fehler (Anzahl)", "Spitze Speicher (MB)", "Spitze CPU (%)", "Sammel-Scan",
         "Fehlerdateien" if mode == KLARTEXT else "Fehlerdateien (Endungen)"], out)


def docs_csv(conn, mode: str) -> str:
    from web.dashboard import _error_reason, _extraction_overview

    ov = _extraction_overview(conn)
    out = [["Übersicht", "", "", ov["total_docs"], "", "", "", "Dokumente gesamt"],
           ["Übersicht", "", "", ov["searchable_total"], "", "", "", "mit durchsuchbarem Inhalt"]]
    for g in ov["format_groups"]:
        for ext, n in g["exts"]:
            out.append(["Nur über Dateiname", g["title"], ext, n, "", "", "", ""])
    for r in ov["read_issues"]:
        out.append(["Fehler bei der Extraktion", "Lesefehler" if r["art"] == "error" else "Nicht gelesen",
                    r["ext"], r["count"], "", "", "", r["reason"]])
    for ext, n in ov["no_text_by_ext"].items():
        out.append(["Ohne Textinhalt", "", ext, n, "", "", "", ""])
    for ext, n in ov["oversized_by_ext"].items():
        out.append(["Zu gross", "", ext, n, "", "", "", ""])
    out.append(["Wartet auf Scan", "", "", ov["pending_total"], "", "", "", ""])
    for cat, files in (("Zu gross", ov["oversized_files"]), ("Ohne Textinhalt", ov["no_text_sample"]),
                       ("Extraktionsfehler", ov["error_files"])):
        for f in files:
            ext = _ext(f.get("filename") or f.get("path"))
            reason = f.get("reason") or (_error_reason(ext, f.get("path")) if cat == "Extraktionsfehler" else "")
            out.append([cat + " (Einzeldatei)", "", ext, 1, f.get("filename", "") if mode == KLARTEXT else "",
                        (f.get("path") or "") if mode == KLARTEXT else "", f.get("filesize", ""), reason])
    return _csv(["Kategorie", "Gruppe", "Endung", "Anzahl", "Dateiname", "Pfad", "Grösse (Bytes)", "Grund"], out)


_LIESMICH = """Archivio – Export der Verläufe
Modus: {modus}

{beschreibung}

Dateien:
  suche.csv                 Such-Verlauf
  scan.csv                  Scan-Verlauf (Dauer, Speicher- und CPU-Spitzen, Fehler)
  dokumentenverarbeitung.csv  Dokumente ohne durchsuchbaren Inhalt
  mcp.csv                   Zugriffe über die Claude/MCP-Schnittstelle

In den Protokollen sind weder Geräte noch Personen gespeichert.
"""


def build_zip(conn, mode: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("LIESMICH.txt", _LIESMICH.format(
            modus="Klartext" if mode == KLARTEXT else "Anonymisiert", beschreibung=BESCHREIBUNG[mode]))
        z.writestr("suche.csv", search_csv(conn, mode))
        z.writestr("scan.csv", scan_csv(conn, mode))
        z.writestr("dokumentenverarbeitung.csv", docs_csv(conn, mode))
        z.writestr("mcp.csv", mcp_csv(conn, mode))
    return buf.getvalue()
