"""Gemeinsame Ressourcen für alle web-Module (Templates, Filter)."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent / "templates")
)


def _office_name() -> str:
    try:
        from config import settings
        return settings.get("office.name", "") or ""
    except Exception:
        return ""


templates.env.globals["office_name"] = _office_name


def _fmt_date(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        y, m, d = iso[:10].split("-")
        return f"{d}.{m}.{y}"
    except ValueError:
        return iso[:10]


def _fmt_datetime(iso: str | None) -> str:
    """Datum + Uhrzeit (lokal). Für 'zuletzt gescannt' — damit ein erneuter Scan
    am selben Tag sichtbar wird."""
    if not iso:
        return "—"
    try:
        from datetime import datetime
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%d.%m.%Y %H:%M")
    except Exception:
        return _fmt_date(iso)


def _fmt_size(n: int) -> str:
    n = n or 0
    if n < 1024:      return f"{n} B"
    if n < 1_048_576: return f"{n / 1024:.0f} KB"
    return f"{n / 1_048_576:.1f} MB"


def _urlencode(v: str) -> str:
    return quote(str(v), safe="")


def _fmt_duration(seconds) -> str:
    """Sekunden -> "45s" / "21min" / "1h 5min" -- Sekundengenauigkeit interessiert
    bei Scans, die Minuten oder Stunden dauern, niemanden (siehe Systemstatus)."""
    if seconds is None:
        return "—"
    try:
        seconds = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}min" if minutes else f"{hours}h"


def _fmt_ms(ms) -> str:
    """Millisekunden -> "180 ms" / "1.2s" -- dieselbe Faustregel wie fmt_duration:
    bei Suchgeschwindigkeit interessiert nicht die exakte Millisekunde, wohl aber
    ob eine Suche spürbar unter oder über einer Sekunde lag (siehe Suche-Protokoll
    im Systemstatus)."""
    if ms is None:
        return "—"
    try:
        ms = int(round(float(ms)))
    except (TypeError, ValueError):
        return "—"
    if ms < 1000:
        return f"{ms} ms"
    return f"{ms / 1000:.1f}s"


templates.env.filters["fmt_date"]     = _fmt_date
templates.env.filters["fmt_datetime"] = _fmt_datetime
templates.env.filters["fmt_size"]     = _fmt_size
templates.env.filters["fmt_duration"] = _fmt_duration
templates.env.filters["fmt_ms"]       = _fmt_ms
templates.env.filters["urlencode"]    = _urlencode
