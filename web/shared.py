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


templates.env.filters["fmt_date"]     = _fmt_date
templates.env.filters["fmt_datetime"] = _fmt_datetime
templates.env.filters["fmt_size"]     = _fmt_size
templates.env.filters["urlencode"]    = _urlencode
