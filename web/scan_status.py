"""Scan-Stand eines Projekts: wie aktuell ist der Index gegenueber dem NAS?

Eine Quelle fuer Startseite (Badge/Chip), Suchtreffer und MCP -- alle zeigen dieselbe
Einstufung. Archivierte Projekte (siehe scanner/scan_log.py) werden absichtlich selten
gescannt und gelten deshalb nie als "veraltet", sondern als "archiviert"."""
from __future__ import annotations

from datetime import datetime, timezone

FRESH_DAYS = 2  # bis zu 2 Tage gilt als aktuell


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def age_label(iso: str | None, now: datetime | None = None) -> str | None:
    dt = _parse(iso)
    if dt is None:
        return None
    secs = ((now or datetime.now(timezone.utc)) - dt).total_seconds()
    if secs < 90:
        return "gerade eben"
    if secs < 3600:
        return f"vor {int(secs // 60)} Min."
    if secs < 86400:
        return f"vor {int(secs // 3600)} Std."
    days = int(secs // 86400)
    if days < 14:
        return f"vor {days} Tg."
    if days < 60:
        return f"vor {days // 7} Wo."
    return f"vor {days // 30} Mon."


def project_scan_state(last_scanned_at: str | None, archive_tier: int = 0,
                       now: datetime | None = None) -> dict:
    """state: never | fresh | stale | archived. `label` ist kurz (Badge), `text` ein
    voller Satz (MCP/Tooltip)."""
    age = age_label(last_scanned_at, now)
    if archive_tier:
        label = f"archiviert · Stand {age}" if age else "archiviert"
        return {"state": "archived", "label": label, "age": age,
                "text": (f"Projekt ist archiviert und wird nur selten neu gescannt "
                         f"(letzter Scan {age}). Neuere Dateien fehlen evtl. im Index."
                         if age else
                         "Projekt ist archiviert und wird nur selten neu gescannt.")}
    dt = _parse(last_scanned_at)
    if dt is None:
        return {"state": "never", "label": "noch nie gescannt", "age": None,
                "text": "Projekt wurde noch nie gescannt, der Index ist unvollständig."}
    days = ((now or datetime.now(timezone.utc)) - dt).total_seconds() / 86400
    if days < FRESH_DAYS:
        return {"state": "fresh", "label": f"gescannt {age}", "age": age,
                "text": f"Index aktuell (letzter Scan {age})."}
    return {"state": "stale", "label": f"gescannt {age}", "age": age,
            "text": (f"Letzter Scan {age} -- Dateien, die seither auf dem Server "
                     f"hinzugekommen oder verschoben wurden, fehlen evtl. im Index.")}


def scan_state_by_id(conn) -> dict[int, dict]:
    rows = conn.execute(
        "SELECT id, name, last_scanned_at, archive_tier FROM projects "
        "WHERE path NOT LIKE 'mailbox:%'"
    ).fetchall()
    out = {}
    for r in rows:
        st = project_scan_state(r["last_scanned_at"], r["archive_tier"] or 0)
        st["name"] = r["name"]
        out[r["id"]] = st
    return out


def scan_state_by_name(conn) -> dict[str, dict]:
    """Fuer Trefferlisten, die nur den Projektnamen kennen. Bei doppelten Namen gewinnt
    der schlechtere Stand -- lieber einmal zu viel warnen als einen alten Index
    verschweigen."""
    rank = {"never": 3, "stale": 2, "archived": 1, "fresh": 0}
    out: dict[str, dict] = {}
    for st in scan_state_by_id(conn).values():
        cur = out.get(st["name"])
        if cur is None or rank[st["state"]] > rank[cur["state"]]:
            out[st["name"]] = st
    return out


def state_for_path(conn, path: str) -> dict | None:
    """Stand des Projekts, in dem `path` liegt (laengster passender Projektpfad)."""
    best = None
    for r in conn.execute(
        "SELECT id, name, path, last_scanned_at, archive_tier FROM projects "
        "WHERE path NOT LIKE 'mailbox:%'"
    ):
        p = r["path"].rstrip("/")
        if path == p or path.startswith(p + "/"):
            if best is None or len(p) > len(best["path"]):
                best = r
    if best is None:
        return None
    st = project_scan_state(best["last_scanned_at"], best["archive_tier"] or 0)
    st["name"] = best["name"]
    return st
