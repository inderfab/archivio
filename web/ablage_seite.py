"""Ablage-Seite des Datei-Drops: `/dashboard/ablage?t=<token>[,<token>…]` (planung/datei-drop-ablage.md §8).

Die Vorschläge sind beim Öffnen schon berechnet (siehe scanner/ablage/vorgang.py); die Seite zeigt sie sofort,
der Spaltenbrowser lädt danach nach. Ablegen läuft im Browser: bestätigen beim Server, dann ausführen beim
Helper (oder beim Server selbst, wenn die Datei am Server-Mac lag).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from config import settings
from db import connection
from scanner.ablage import vorgang
from web.shared import templates

log = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard")

MAX_DATEIEN = 25


def _badge(p: float) -> tuple[str, str]:
    if p >= 0.9:
        return "sicher", "ok"
    if p >= 0.6:
        return "wahrscheinlich", "mid"
    return "unsicher", "low"


def _teile(pfad: str, basis: str) -> list[str]:
    pfad, basis = pfad.rstrip("/"), basis.rstrip("/")
    return [t for t in pfad[len(basis):].split("/") if t] if pfad.startswith(basis) else []


def _ansicht(idx: int, v: dict) -> dict:
    """Alles, was das Template für eine Datei braucht (kein Rechnen im Template)."""
    vs = v["vorschlag"] or {}
    projekte = vs.get("projekte") or []
    fall = vs.get("fall", "projekt_unklar")
    top = projekte[0] if projekte else None
    d: dict = {"idx": idx, "token": v["token"], "dateiname": v["dateiname"], "status": v["status"], "fall": fall,
               "lokal": bool(v.get("lokal_src")), "name_vorschlag": vs.get("dateiname_vorschlag"),
               "projekte": projekte, "optionen": [], "krumen": [], "sicher_pfad": "", "start_pfad": "",
               "projekt": None, "duplikate": vs.get("duplikate") or [],
               "angeboten": vs.get("projekte_angeboten") or [], "angeglichen": bool(vs.get("angeglichen"))}
    d["vorbereitung"] = fall == "vorbereitung"
    if fall in ("duplikat", "projekt_unklar", "vorbereitung") or not top:
        base = settings.get("scanner.base_folders", []) or []
        d["start_pfad"] = base[0]["path"] if base else ""
        return d
    conn = connection.get_connection()
    try:
        row = conn.execute("SELECT id, name, path FROM projects WHERE id = ?", (top["id"],)).fetchone()
    finally:
        conn.close()
    if not row:
        return d
    label, klasse = _badge(top["p"])
    d["projekt"] = {"id": row["id"], "name": row["name"], "path": row["path"], "p": top["p"], "badge": label,
                    "klasse": klasse, "gruende": top.get("gruende") or []}
    sicher = (vs.get("sicher_bis") or {}).get("pfad") or row["path"]
    d["sicher_pfad"] = sicher
    d["start_pfad"] = row["path"]
    d["krumen"] = [row["name"]] + _teile(sicher, row["path"])
    for o in vs.get("optionen") or []:
        rel = _teile(o["pfad"], sicher)
        lab, kl = _badge(o["p"])
        d["optionen"].append({**o, "rel": " › ".join(rel) if rel else Path(o["pfad"]).name,
                              "badge": lab, "klasse": kl, "prozent": round(o["p"] * 100)})
    return d


def _tokens(t: str) -> list[str]:
    res = []
    for x in (t or "").split(","):
        x = x.strip()
        if vorgang.token_gueltig(x) and x not in res:
            res.append(x)
    return res[:MAX_DATEIEN]


def _laden(tokens: list[str]) -> list[dict]:
    conn = connection.get_connection()
    try:
        for t in tokens:
            vorgang.vorschlag_nachholen(conn, t)             # war der Kontext noch nicht bereit: jetzt, ohne zu blockieren
        vorgang.gruppe_angleichen(conn, tokens)
        return [v for v in (vorgang.holen(conn, t) for t in tokens) if v]
    finally:
        conn.close()


@router.get("/ablage", response_class=HTMLResponse)
async def ablage_seite(request: Request, t: str = Query("")):
    tokens = _tokens(t)
    vorgaenge = await asyncio.to_thread(_laden, tokens) if tokens else []
    ansichten = [_ansicht(i, v) for i, v in enumerate(vorgaenge)]
    return templates.TemplateResponse("dashboard_ablage.html", {
        "request": request, "dateien": ansichten,
        "abgelaufen": bool(tokens) and not ansichten, "ohne_token": not tokens,
    })


@router.get("/ablage/datei", response_class=HTMLResponse)
async def ablage_datei(request: Request, token: str = Query(...), idx: int = Query(0),
                       projekt: int = Query(0)):
    """Eine Datei neu darstellen, nachdem von Hand ein Projekt gewählt wurde (htmx-Austausch)."""
    def _tun():
        conn = connection.get_connection()
        try:
            if projekt:
                vorgang.projekt_waehlen(conn, token, projekt)
            else:
                vorgang.vorschlag_nachholen(conn, token)
            return vorgang.holen(conn, token)
        finally:
            conn.close()
    v = await asyncio.to_thread(_tun)
    if v is None:
        return HTMLResponse("<div class='ablage-fehler'>Vorgang abgelaufen.</div>", status_code=404)
    return templates.TemplateResponse("_ablage_datei.html", {"request": request, "d": _ansicht(idx, v)})


@router.get("/ablage-spalten", response_class=HTMLResponse)
async def ablage_spalten(request: Request, start: str = Query(...), dest: str = Query(""), idx: int = Query(0)):
    """Spaltenbrowser, bis zum Vorschlag aufgeklappt. Lädt nach, damit die Seite nicht auf das NAS wartet."""
    from web.dashboard import _list_browse_level, _prerender_browse_columns
    if not dest or dest.rstrip("/") == start.rstrip("/"):      # nichts aufzuklappen: erste Ebene zeigen
        subdirs, error_msg = await asyncio.to_thread(_list_browse_level, start)
        return templates.TemplateResponse("_dashboard_browse.html", {
            "request": request, "subdirs": subdirs, "error_msg": error_msg, "current_path": start,
            "project_id": idx, "depth": 0, "selected": ""})
    html = await asyncio.to_thread(_prerender_browse_columns, request, start, dest, idx)
    return HTMLResponse(html)
