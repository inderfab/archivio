"""
CLI-Einstiegspunkt.

Aufruf aus Projektverzeichnis:  python __main__.py scan ...
Aufruf aus übergeordnetem Dir:  python -m archivio scan ...
"""
from __future__ import annotations

import sys
from pathlib import Path

# Projektverzeichnis in sys.path eintragen, damit Imports funktionieren
# egal ob via `python __main__.py` oder `python -m archivio`
_ROOT = Path(__file__).parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import argparse
import logging

from db import connection, queries
from scanner.walker import scan_project


def cmd_scan(args: argparse.Namespace):
    connection.init_schema()
    path = Path(args.path)

    if not path.exists():
        sys.exit(f"Fehler: Pfad nicht gefunden: {path}")

    conn = connection.get_connection()
    project_id = _resolve_project(conn, args)
    name = conn.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()["name"]
    conn.close()

    print(f"[Archivio] Projekt #{project_id} — {name}")
    print(f"           Pfad: {path}")
    print()

    scan_project(project_id, path)

    conn2 = connection.get_connection()
    rows = conn2.execute(
        "SELECT extraction_status, COUNT(*) n FROM documents WHERE project_id=? GROUP BY extraction_status",
        (project_id,),
    ).fetchall()
    conn2.close()

    total = sum(r["n"] for r in rows)
    print(f"\nErgebnis: {total} Dateien indexiert")
    for r in rows:
        print(f"  {r['extraction_status']:12} {r['n']}")


def cmd_projects(_args: argparse.Namespace):
    connection.init_schema()
    conn = connection.get_connection()
    rows = conn.execute("SELECT id, name, path, active FROM projects ORDER BY id").fetchall()
    conn.close()

    if not rows:
        print("Keine Projekte in der Datenbank.")
        return
    for r in rows:
        status = "aktiv" if r["active"] else "inaktiv"
        print(f"[{r['id']}] {r['name']}  ({status})")
        print(f"     {r['path']}")


def cmd_init(_args: argparse.Namespace):
    """Projekte aus config.yaml in die Datenbank übernehmen."""
    from config import settings
    connection.init_schema()
    projects = settings.get("projects", [])
    if not projects:
        print("Keine Projekte in config.yaml definiert.")
        return
    conn = connection.get_connection()
    for p in projects:
        if not p.get("active", True):
            continue
        with conn:
            pid = queries.insert_project(conn, p["name"], p["path"])
        print(f"  Projekt #{pid}: {p['name']}")
    conn.close()
    print("Fertig.")


def _resolve_project(conn, args: argparse.Namespace) -> int:
    """Bestehendes Projekt per ID finden oder neu anlegen."""
    if getattr(args, "project_id", None):
        row = conn.execute("SELECT id FROM projects WHERE id=?", (args.project_id,)).fetchone()
        if row:
            return row["id"]
        print(f"Hinweis: Projekt #{args.project_id} nicht gefunden — lege neu an.")

    name = getattr(args, "name", None) or Path(args.path).name
    with conn:
        return queries.insert_project(conn, name, str(Path(args.path)))


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    parser = argparse.ArgumentParser(
        prog="archivio",
        description="Archivio — Interne Wissensplattform",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- scan ---
    p_scan = sub.add_parser("scan", help="Verzeichnis scannen und indexieren")
    p_scan.add_argument("path", help="Zu scannender Pfad")
    p_scan.add_argument("--project-id", type=int, metavar="ID",
                        help="Bestehendes Projekt verwenden")
    p_scan.add_argument("--name", metavar="NAME",
                        help="Projektname beim Neuanlegen (Standard: Verzeichnisname)")

    # --- projects ---
    sub.add_parser("projects", help="Alle Projekte in der DB auflisten")

    # --- init ---
    sub.add_parser("init", help="Projekte aus config.yaml in die DB übernehmen")

    args = parser.parse_args()
    {"scan": cmd_scan, "projects": cmd_projects, "init": cmd_init}[args.command](args)


if __name__ == "__main__":
    main()
