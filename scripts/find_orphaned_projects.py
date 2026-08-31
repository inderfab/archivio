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

Standardmässig nur ein Bericht (nichts wird verändert) -- die einfache Heuristik
(Elternordner ist kein aktueller Base-Folder) erwischt daneben auch normale, echte
Projekte, die nur zufällig tiefer verschachtelt liegen als ein direkter Base-Folder-
Unterordner (z.B. "Projekte/000 Archivprojekte/<name>" statt "Projekte/<name>") --
jede Zeile vor dem Abschalten prüfen! "FEHLT AUF DISK" mit wenigen/keinen Dokumenten
ist ein starkes Indiz für einen echten Karteileichen-Eintrag; "existiert" mit
tausenden Dokumenten fast immer ein echtes, aktives Projekt.

Zum gezielten Abschalten einzelner Zeilen (empfohlen): --id 18 (mehrfach möglich,
z.B. --id 18 --id 42) -- deaktiviert NUR die genannten IDs, alle anderen bleiben
unangetastet, egal wie viele insgesamt in der Liste stehen.
Zum Abschalten ALLER oben gelisteten Zeilen auf einmal: --deactivate (nur verwenden,
wenn wirklich jede einzelne Zeile geprüft und als Karteileiche bestätigt wurde).
Dokumente/Index bleiben in beiden Fällen erhalten und durchsuchbar, nur künftige
Scans überspringen die deaktivierten Projekte.

Aufruf:
    .venv/bin/python scripts/find_orphaned_projects.py               # nur Bericht
    .venv/bin/python scripts/find_orphaned_projects.py --id 18       # nur ID 18 abschalten
    .venv/bin/python scripts/find_orphaned_projects.py --deactivate  # ALLE gelisteten abschalten
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main() -> None:
    from config import settings
    from db import connection

    args = sys.argv[1:]
    apply_all = "--deactivate" in args
    only_ids: set[int] = set()
    for i, a in enumerate(args):
        if a == "--id" and i + 1 < len(args):
            try:
                only_ids.add(int(args[i + 1]))
            except ValueError:
                print(f"Ungültige --id: {args[i + 1]!r} (muss eine Zahl sein)")
                return

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
          "jede Zeile vor dem Abschalten prüfen!)\n")
    for r in orphans:
        count = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE project_id=?", (r["id"],)
        ).fetchone()[0]
        exists = "existiert" if Path(r["path"]).exists() else "FEHLT AUF DISK"
        print(f"  [{r['id']}] {r['name']!r} — {r['path']} ({count} Dokumente, {exists})")

    if only_ids:
        valid_ids = {r["id"] for r in orphans}
        unknown = only_ids - valid_ids
        if unknown:
            print(f"\n--id {sorted(unknown)} steht/stehen nicht in der obigen Liste — "
                  "aus Sicherheitsgründen wird nichts geändert.")
            conn.close()
            return
        print(f"\n--id gesetzt — deaktiviere nur {sorted(only_ids)}…")
        with conn:
            conn.executemany(
                "UPDATE projects SET active=0 WHERE id=?", [(i,) for i in only_ids]
            )
        print(f"{len(only_ids)} Projekt(e) deaktiviert. Dokumente/Index bleiben erhalten.")
    elif apply_all:
        print("\n--deactivate gesetzt — deaktiviere ALLE oben gelisteten Projekte…")
        with conn:
            conn.executemany(
                "UPDATE projects SET active=0 WHERE id=?", [(r["id"],) for r in orphans]
            )
        print(f"{len(orphans)} Projekt(e) deaktiviert. Dokumente/Index bleiben erhalten.")
    else:
        print("\nNur Bericht -- nichts geändert. Zum gezielten Abschalten: --id <ID> "
              "(mehrfach möglich). Zum Abschalten ALLER oben gelisteten: --deactivate.")

    conn.close()


if __name__ == "__main__":
    main()
