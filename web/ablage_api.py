"""HTTP-Schnittstelle des Datei-Drops (planung/datei-drop-ablage.md §3).

Helper → /analyse (Upload kleiner Textformate oder nur Metadaten) → Token
Browser → /{token}/bestaetigen (der Server prüft: Ziel liegt in einem Projekt)
Helper → /{token}/entscheid (holt dest/dateiname VOM SERVER) → /{token}/abgelegt
Server-Mac → /analyse-lokal und /{token}/ausfuehren-lokal (nur von localhost)
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from db import connection
from scanner.ablage import vorgang

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ablage")

_MAX_UPLOAD = 60 * 1024 * 1024
_LOKAL = ("127.0.0.1", "::1", "localhost", "testclient")


def _fehler(code: int, text: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": text}, status_code=code)


def _nur_lokal(request: Request) -> bool:
    return bool(request.client) and request.client.host in _LOKAL


def _arbeit(fn, *args):
    """Blockierende DB-/Dateiarbeit in einem Thread, mit eigener Verbindung."""
    conn = connection.get_connection()
    try:
        return fn(conn, *args)
    finally:
        conn.close()


@router.post("/analyse")
async def analyse(name: str = Form(...), size: int = Form(0), mtime: str = Form(""), hash: str = Form(""),
                  host: str = Form(""), datei: UploadFile | None = File(None)):
    daten = None
    if datei is not None:
        daten = await datei.read(_MAX_UPLOAD + 1)
        if len(daten) > _MAX_UPLOAD:
            return _fehler(413, "Datei zu gross für den Upload")
    erg = await asyncio.to_thread(_arbeit, lambda c: vorgang.analyse_upload(
        c, name, size or None, mtime or None, hash or None, host or None, daten))
    return {"ok": True, "token": erg["token"], "fall": erg["vorschlag"]["fall"]}


@router.post("/analyse-lokal")
async def analyse_lokal(request: Request):
    """Drop direkt am Server-Mac: nur von dort (localhost), der Server liest die Datei selbst."""
    if not _nur_lokal(request):
        return _fehler(403, "Nur vom Server-Mac selbst")
    body = await request.json()
    try:
        erg = await asyncio.to_thread(_arbeit, lambda c: vorgang.analyse_lokal(c, str(body.get("src") or "")))
    except FileNotFoundError as exc:
        return _fehler(404, str(exc))
    return {"ok": True, "token": erg["token"], "fall": erg["vorschlag"]["fall"]}


@router.get("/{token}")
async def vorgang_lesen(token: str):
    v = await asyncio.to_thread(_arbeit, lambda c: vorgang.holen(c, token))
    if v is None:
        return _fehler(404, "Unbekannter Vorgang")
    return {"ok": True, "token": token, "status": v["status"], "dateiname": v["dateiname"], "host": v["host"],
            "vorschlag": v["vorschlag"], "entscheid": v["entscheid"], "lokal": bool(v.get("lokal_src"))}


@router.post("/{token}/bestaetigen")
async def bestaetigen(token: str, request: Request):
    body = await request.json()
    ok, fehler = await asyncio.to_thread(_arbeit, lambda c: vorgang.bestaetigen(c, token, body))
    return {"ok": True} if ok else _fehler(400, fehler)


@router.get("/{token}/entscheid")
async def entscheid(token: str):
    e = await asyncio.to_thread(_arbeit, lambda c: vorgang.entscheid_fuer_helper(c, token))
    return e if e is not None else _fehler(404, "Unbekannter Vorgang")


@router.post("/{token}/abgelegt")
async def abgelegt(token: str, request: Request):
    body = await request.json()
    ok, fehler = await asyncio.to_thread(
        _arbeit, lambda c: vorgang.abgelegt(c, token, body.get("final_path"), bool(body.get("trocken"))))
    return {"ok": True} if ok else _fehler(409, fehler)


@router.post("/{token}/abbrechen")
async def abbrechen(token: str):
    await asyncio.to_thread(_arbeit, lambda c: vorgang.abbrechen(c, token))
    return {"ok": True}


@router.post("/{token}/ausfuehren-lokal")
async def ausfuehren_lokal(token: str, request: Request):
    """Drop am Server-Mac: der Server verschiebt/kopiert selbst (keine Helper-Bridge nötig)."""
    if not _nur_lokal(request):
        return _fehler(403, "Nur vom Server-Mac selbst")

    def _tun(conn):
        v = vorgang.holen(conn, token)
        if v is None or not v.get("lokal_src"):
            return 404, {"ok": False, "error": "Kein lokaler Vorgang"}
        if v["status"] != "bestaetigt":
            return 409, {"ok": False, "error": "Ablage ist nicht bestätigt"}
        th = vorgang._transport()
        e = v["entscheid"]
        if not th.ist_unterhalb(e["dest"], e["projekt_pfad"]):
            return 403, {"ok": False, "error": "Zielordner liegt ausserhalb des Projekts"}
        trocken = th.trocken()
        try:
            erg = th.datei_ablegen(v["lokal_src"], e["dest"], e["dateiname"], bool(e["kopie"]),
                                   e.get("vorgaenger_modus") or "behalten", e.get("vorgaenger"),
                                   e.get("archiv_ordner"), nur_planen=trocken)
        except FileNotFoundError as exc:
            return 404, {"ok": False, "error": str(exc)}
        except (OSError, ValueError) as exc:
            return 500, {"ok": False, "error": str(exc)}
        vorgang.abgelegt(conn, token, erg["final"], trocken)
        return 200, {"ok": True, "final_path": erg["final"], "trocken": trocken, "archiviert": erg["archiviert"]}

    code, body = await asyncio.to_thread(_arbeit, _tun)
    return JSONResponse(body, status_code=code)
