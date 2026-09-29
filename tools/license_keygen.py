#!/usr/bin/env python3
"""Lokales Werkzeug für Fabio zur Verwaltung des Archivio-Lizenzsystems.

Läuft NICHT im Archivio-Server selbst, sondern wird von Hand auf dem eigenen
Rechner ausgeführt -- das Geheimnis darf niemals ins Repo oder in eine öffentlich
einsehbare Stelle gelangen (im ausgelieferten App-Bundle steckt es zwangsläufig,
siehe Sicherheitsmodell in scanner/license.py).

Einmalig, vor dem ersten Verkauf:

    .venv/bin/python3 tools/license_keygen.py generate-secret

  Erzeugt ein neues Geheimnis und schreibt es in license_secret.txt im aktuellen
  Verzeichnis -- diese Datei sofort an einen sicheren, nicht synchronisierten Ort
  verschieben (z.B. Passwort-Manager) und NICHT im Archivio-Repo belassen (steht
  daher in .gitignore). Der ausgegebene Wert kommt einmalig in
  scanner/license.py::LICENSE_SECRET_B64.

  Nur EIN Mal ausführen: ein neues Geheimnis würde alle bereits ausgegebenen
  Lizenzschlüssel ungültig machen.

Bei jedem Verkauf/jeder Verlängerung:

    .venv/bin/python3 tools/license_keygen.py issue \\
        --buero "Musterbüro AG" --jahre 1 \\
        --secret-file /pfad/zu/license_secret.txt

  Gibt den fertigen Lizenzschlüssel aus (Format ARCH-XXXX-XXXX-...), den du per
  Mail an den Kunden schickst. Der Büro-Name dient nur der Anzeige hier -- er
  steht bewusst nicht im Schlüssel selbst (siehe scanner/license.py), halte ihn
  in deinen eigenen Verkaufsunterlagen fest.
"""
from __future__ import annotations

import argparse
import base64
import secrets
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from scanner.license import build_license_bytes, format_license_key  # noqa: E402


def cmd_generate_secret(args: argparse.Namespace) -> None:
    out_path = Path(args.out)
    if out_path.exists() and not args.force:
        print(f"Abgebrochen: {out_path} existiert bereits (--force zum Überschreiben).")
        sys.exit(1)

    secret_bytes = secrets.token_bytes(32)
    secret_b64 = base64.b64encode(secret_bytes).decode("ascii")

    out_path.write_text(secret_b64 + "\n", encoding="utf-8")
    out_path.chmod(0o600)

    print(f"Geheimnis geschrieben nach: {out_path.resolve()}")
    print("  -> JETZT an einen sicheren, nicht synchronisierten Ort verschieben")
    print("     (Passwort-Manager oder verschlüsseltes externes Laufwerk).")
    print("  -> Diese Datei NIE ins Archivio-Repo committen.")
    print()
    print("Kommt einmalig in scanner/license.py:")
    print()
    print(f'    LICENSE_SECRET_B64 = "{secret_b64}"')
    print()


def cmd_issue(args: argparse.Namespace) -> None:
    secret_b64 = Path(args.secret_file).read_text(encoding="utf-8").strip()
    secret = base64.b64decode(secret_b64)

    ausgestellt = date.today()
    gueltig_bis = ausgestellt.replace(year=ausgestellt.year + args.jahre)

    raw = build_license_bytes(secret, ausgestellt, gueltig_bis)
    key_str = format_license_key(raw)

    print()
    print(f"Büro (nur zur Anzeige, nicht im Schlüssel enthalten): {args.buero}")
    print(f"Ausgestellt: {ausgestellt.isoformat()}")
    print(f"Gültig bis:  {gueltig_bis.isoformat()}")
    print()
    print(key_str)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate-secret", help="Einmalig: neues Geheimnis erzeugen")
    p_gen.add_argument("--out", default="license_secret.txt", help="Zieldatei für das Geheimnis")
    p_gen.add_argument("--force", action="store_true", help="Bestehende Datei überschreiben")
    p_gen.set_defaults(func=cmd_generate_secret)

    p_issue = sub.add_parser("issue", help="Einen neuen Lizenzschlüssel ausstellen")
    p_issue.add_argument("--buero", required=True, help="Name des Büros (nur zur Anzeige)")
    p_issue.add_argument("--jahre", type=int, default=1, help="Gültigkeitsdauer in Jahren (Standard 1)")
    p_issue.add_argument("--secret-file", required=True, help="Pfad zur Datei mit dem Geheimnis")
    p_issue.set_defaults(func=cmd_issue)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
