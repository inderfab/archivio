# Archivio

Interne Wissensplattform für ein Architekturbüro. Vollständig lokal, kein Cloud-Dienst.

## Schnellstart

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Datenbank initialisieren
sqlite3 archivio.db < db/schema.sql

# Server starten
uvicorn web.main:app --reload
```

## Konfiguration

Alle Einstellungen in `config.yaml`. Der Datenbankpfad, Scanner-Optionen und
Server-Bindung sind dort zentral konfigurierbar.
