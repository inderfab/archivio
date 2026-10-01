"""Datenbank-Abfragen, die der Vorschlag pro Datei braucht und die nicht im Kontext im Speicher liegen.

Jede Abfrage öffnet eine eigene, kurze Verbindung (der Kontext ist gecacht und lebt länger als jede
Verbindung). Alles ist gezielt und indiziert: nie ein Durchlauf über das NAS (`exists()` nur auf den
Ergebnissen, nicht hier).
"""
from __future__ import annotations

import logging
import os
import re
from collections import Counter

from db import connection
from scanner.ablage import merkmale as mk
from scanner.ablage.normalisieren import pfad_schluessel

log = logging.getLogger(__name__)
_LIMIT = 800


def _laengstes_wort(vname: str) -> str:
    kern = vname.rsplit(".", 1)[0] if "." in vname else vname
    woerter = sorted(re.findall(r"[^\W\d_]{3,}", kern), key=len, reverse=True)
    return woerter[0] if woerter else (kern.split() or [""])[0]


def haken_setzen(ctx, conn=None) -> None:
    pfad_ordner = {o.path: o.id for o in ctx.ordner.values()}

    def vorgaenger(vname: str, projekt_id):
        if not vname:
            return []
        ext = "." + vname.rsplit(".", 1)[1] if "." in vname else ""
        wort = _laengstes_wort(vname)
        if not wort:
            return []
        c = connection.get_connection()
        try:
            sql = ("SELECT d.project_id, dp.path, d.filename FROM document_paths dp "
                   "JOIN documents d ON d.id = dp.document_id "
                   "WHERE dp.is_primary = 1 AND d.filename LIKE ? ESCAPE '\\'")
            args: list = ["%" + wort.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"]
            if ext:
                sql += " AND d.extension = ?"
                args.append(ext)
            if projekt_id is not None:
                sql += " AND d.project_id = ?"
                args.append(projekt_id)
            sql += f" LIMIT {_LIMIT}"
            res = []
            for r in c.execute(sql, args):
                if mk.vorgaenger_name(r["filename"]) != vname:
                    continue
                oid = pfad_ordner.get(pfad_schluessel(os.path.dirname(r["path"])))
                if oid is not None:
                    res.append((r["project_id"], oid, r["path"], r["filename"]))
            return res
        finally:
            c.close()

    def duplikat(h: str):
        c = connection.get_connection()
        try:
            return [(r["project_id"], r["path"]) for r in c.execute(
                "SELECT d.project_id, dp.path FROM documents d JOIN document_paths dp ON dp.document_id = d.id "
                "WHERE d.hash = ? LIMIT 5", (h,))]
        finally:
            c.close()

    def fts(woerter: list[str]):
        c = connection.get_connection()
        try:
            q = " OR ".join(f'"{w}"' for w in woerter)
            rows = c.execute(
                "SELECT d.project_id, COUNT(*) AS n FROM chunks_fts JOIN document_chunks dc ON chunks_fts.rowid = dc.id "
                "JOIN documents d ON d.id = dc.document_id WHERE chunks_fts MATCH ? AND d.project_id IS NOT NULL "
                "GROUP BY d.project_id ORDER BY n DESC LIMIT 3", (q,)).fetchall()
            gesamt = sum(r["n"] for r in rows) or 1
            return {r["project_id"]: r["n"] / gesamt for r in rows}
        except Exception as exc:
            log.debug("Volltext-Rückfall übersprungen: %s", exc)
            return {}
        finally:
            c.close()

    def domain(dom: str):
        c = connection.get_connection()
        try:
            z = Counter()
            for r in c.execute("SELECT d.project_id AS p, COUNT(*) AS n FROM mails m JOIN documents d ON d.id = m.document_id "
                               "WHERE m.sender LIKE ? GROUP BY d.project_id", (f"%@{dom}%",)):
                z[r["p"]] = r["n"]
            gesamt = sum(z.values())
            return {p: n / gesamt for p, n in z.items()} if gesamt >= 3 else {}
        finally:
            c.close()

    def host(h: str):
        c = connection.get_connection()
        try:
            z = Counter()
            for r in c.execute("SELECT gewaehlt_pfad FROM ablage_log WHERE host = ? AND ts >= datetime('now','-14 day')",
                               (h,)):
                for pid, p in ctx.projekte.items():
                    if r["gewaehlt_pfad"] and pfad_schluessel(r["gewaehlt_pfad"]).startswith(pfad_schluessel(p.path) + "/"):
                        z[pid] += 1
            gesamt = sum(z.values())
            return {p: n / gesamt for p, n in z.items()} if gesamt else {}
        except Exception:
            return {}            # Tabelle kommt erst mit dem Transport/Lernen (Etappe 6/8)
        finally:
            c.close()

    ctx.vorgaenger_fn, ctx.duplikat_fn, ctx.fts_fn, ctx.domain_fn, ctx.host_fn = vorgaenger, duplikat, fts, domain, host
