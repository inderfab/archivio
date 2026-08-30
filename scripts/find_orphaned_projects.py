#!/usr/bin/env python3
"""Findet aktive Projekte, deren übergeordneter Ordner KEIN aktuell konfigurierter
Base-Folder (Einstellungen → Scanner → Base-Folders) mehr ist.

Hintergrund: Ordner unter einem Base-Folder werden per Toggle in der Fotos-Übersicht
als Projekt aktiviert (web/dashboard.py::toggle_project). Wird der Base-Folder später
in den Einstellungen entfernt oder geändert, blieb das zugehörige Projekt bisher
active=1 in der DB -- unsichtbar in Einstellungen (zeigt nur aktuelle Base-Folders)
UND im Dashboard (Gruppierung läuft nur über aktuelle Base-Folders), aber
/api/scan/all und der geplante Scan holten weiterhin blind "WHERE active=1" und
scannten das Geisterprojekt für immer weiter. web/dashboard.py::settings_save()
deaktiviert das ab sofort automatisch BEIM Ändern eines Base-Folders -- dieses
Skript räumt bereits VOR diesem Fix entstandene Altfälle einmalig auf.

Standardmässig nur ein Bericht (nichts wird verändert) -- die Liste enthält
möglicherweise auch normale, nicht über Base-Folders verwaltete Projekte (z.B.
Architekturprojekte auf dem NAS), also jede Zeile vor --deactivate prüfen.
Mit --deactivate werden die gefundenen Projekte auf active=0 gesetzt (Dokumente/
Index bleiben erhalten und durchsuchbar, nur künftige Scans überspringen sie).

Aufruf:
    .venv/bin/python scripts/find_orphaned_projects.py               # nur Bericht
    .venv/bin/python scripts/find_orphaned_projects.py --deactivate  # wirklich abschalten
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main() -> None:
    from config import settings
    from db import connection

    apply_changes = "--deactivate" in sys.argv[1:]

    base_paths = {
        f.get("path") for f in (settings.get("scanner.base_folders") or []) if f.get("path")
    }
    if not base_paths:
        print("Kein Base-Folder konfiguriert (Einstellungen → Scanner) — nichts zu prüfen.")
        return

    print("Aktuell konfigurierte Base-Folders:")
    for p in sorted(base_paths):
        print(f"  - {p}")
    print()

    conn = connection.get_connection()
    rows = conn.execute("SELECT id, name, path FROM projects WHERE active=1").fetchall()
    orphans = [r for r in rows if os.path.dirname(r["path"]) not in base_paths]

    if not orphans:
        print("Keine verwaisten Projekte gefunden.")
        conn.close()
        return

    print(f"{len(orphans)} aktive Projekte, deren Elternordner KEIN aktueller Base-Folder ist:")
    print("(schliesst normale, nicht über Base-Folders verwaltete Projekte mit ein -- "
          "vor --deactivate jede Zeile prüfen!)\n")
    for r in orphans:
        count = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE project_id=?", (r["id"],)
        ).fetchone()[0]
        exists = "existiert" if Path(r["path"]).exists() else "FEHLT AUF DISK"
        print(f"  [{r['id']}] {r['name']!r} — {r['path']} ({count} Dokumente, {exists})")

    if apply_changes:
        print("\n--deactivate gesetzt — deaktiviere die obigen Projekte…")
        with conn:
            conn.executemany(
                "UPDATE projects SET active=0 WHERE id=?", [(r["id"],) for r in orphans]
            )
        print(f"{len(orphans)} Projekt(e) deaktiviert. Dokumente/Index bleiben erhalten.")
    else:
        print("\nNur Bericht -- nichts geändert. Erneut mit --deactivate ausführen, um die "
              "obigen Projekte abzuschalten.")

    conn.close()


if __name__ == "__main__":
    main()
