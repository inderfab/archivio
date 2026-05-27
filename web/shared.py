"""Gemeinsame Ressourcen für alle web-Module (Templates, Filter)."""
from __future__ import annotations

from urllib.parse import quote
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="web/templates")


def _fmt_date(iso: str | None) -> str:
    if not iso:
        return "—"
    return iso[:10]


def _fmt_size(n: int) -> str:
    n = n or 0
    if n < 1024:      return f"{n} B"
    if n < 1_048_576: return f"{n / 1024:.0f} KB"
    return f"{n / 1_048_576:.1f} MB"


def _urlencode(v: str) -> str:
    return quote(str(v), safe="")


templates.env.filters["fmt_date"]  = _fmt_date
templates.env.filters["fmt_size"]  = _fmt_size
templates.env.filters["urlencode"] = _urlencode
