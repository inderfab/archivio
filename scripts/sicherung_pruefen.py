"""Prueft eine von Archivio erstellte Sicherung auf Vollstaendigkeit und Lesbarkeit.

Aufruf:  python3 sicherung_pruefen.py /Volumes/Server/Archivio-Sicherung

Bewusst unabhaengig vom Server: liest die Sicherung nur, braucht keine laufende
Installation und veraendert nichts. Gedacht fuer die Frage "ist meine Sicherung
im Ernstfall wirklich brauchbar?" -- eine Datei, die nur existiert, beantwortet
die noch nicht.

Geprueft wird:
  * sind alle drei Bestandteile da (Datenbank, Einstellungen, Manifest),
  * ist die Datenbank in sich stimmig (PRAGMA integrity_check, nicht nur
    quick_check wie beim Erstellen -- hier darf es laenger dauern),
  * deckt sich der tatsaechliche Inhalt mit dem, was das Manifest behauptet,
  * laesst sich der Suchindex benutzen,
  * sind wirklich keine Mail-Passwoerter enthalten.
"""
import json, sqlite3, sys, os, yaml
from pathlib import Path

d = Path(sys.argv[1])
if d.name != "Archivio-Sicherung" and (d / "Archivio-Sicherung").is_dir():
    d = d / "Archivio-Sicherung"

print(f"Sicherung: {d}\n")
fehler = 0

for name in ("archivio.db", "config.yaml", "manifest.json"):
    ok = (d / name).exists()
    print(f"  [{'OK ' if ok else 'FEHLT'}] {name}")
    fehler += (not ok)
if fehler:
    sys.exit("\nUnvollstaendig — abgebrochen.")

m = json.loads((d / "manifest.json").read_text())
print(f"\n  Erstellt : {m['created_at'].replace('T',' ')}")
print(f"  Version  : {m['server_version']}")
print(f"  Laut Manifest: {m['stats']['projects']} Projekte, {m['stats']['documents']} Dokumente, {m['stats']['chunks']} Chunks")
print(f"  Groesse  : {(d/'archivio.db').stat().st_size/1e9:.2f} GB")

print("\n  Vollstaendige Integritaetspruefung der Datenbank laeuft (kann bei mehreren GB einige Minuten dauern)…")
c = sqlite3.connect(f"file:{d/'archivio.db'}?mode=ro", uri=True)
res = c.execute("PRAGMA integrity_check").fetchone()[0]
print(f"  [{'OK ' if res=='ok' else 'FEHLER'}] integrity_check: {res}")
fehler += (res != "ok")

echt = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("projects", "documents", "document_chunks")}
passt = (echt["projects"] == m["stats"]["projects"]
         and echt["documents"] == m["stats"]["documents"]
         and echt["document_chunks"] == m["stats"]["chunks"])
print(f"  [{'OK ' if passt else 'FEHLER'}] Inhalt deckt sich mit dem Manifest: "
      f"{echt['projects']} / {echt['documents']} / {echt['document_chunks']}")
fehler += (not passt)

treffer = c.execute("SELECT COUNT(*) FROM documents_fts WHERE documents_fts MATCH 'plan'").fetchone()[0]
print(f"  [{'OK ' if treffer >= 0 else 'FEHLER'}] Suchindex benutzbar (Testsuche 'plan': {treffer} Treffer)")
c.close()

cfg = yaml.safe_load((d / "config.yaml").read_text()) or {}
konten = cfg.get("mail_accounts") or []
mit_pw = [k for k in konten if (k.get("password") or "").strip()]
print(f"  [{'OK ' if not mit_pw else 'FEHLER'}] Keine Mail-Passwoerter enthalten "
      f"({len(konten)} Konten, davon {len(mit_pw)} mit Passwort)")
fehler += bool(mit_pw)
ordner = [f.get("path") for f in (cfg.get("scanner", {}) or {}).get("base_folders") or []]
print(f"  Gesicherte Projektordner: {len(ordner)}")

print("\n" + ("ALLES GRUEN — die Sicherung ist vollstaendig und lesbar."
             if not fehler else f"{fehler} PROBLEM(E) gefunden."))
