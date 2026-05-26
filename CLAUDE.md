# Archivio — Interne Wissensplattform für Architekturbüro

## Stack
- **Backend**: Python 3.12, FastAPI
- **Datenbank**: SQLite mit FTS5 (Volltextsuche)
- **Frontend**: HTMX + Jinja2 Templates
- **Paketmanagement**: pip + requirements.txt

## Prinzipien
- Vollständig lokal — kein Cloud-Dienst, keine externe API
- NAS ist nur Ablage; alle Verarbeitung findet lokal statt
- Dateiidentität über SHA256-Hash (nicht Pfad) — Duplikate und Verschiebungen werden erkannt
- Fehlertoleranz bei Extraktion: jede Datei hat ein `extraction_status`-Feld (`pending`, `ok`, `error`, `unsupported`)
- Kein Datenverlust bei fehlerhaften Dateien — Fehler werden geloggt, der Rest läuft weiter

## Projektstruktur
```
archivio/
  scanner/    # Dateiscanner, Hash-Berechnung, Extraktion (PDF, DWG, Mail, …)
  web/        # FastAPI-App, Routen, Jinja2-Templates
  db/         # Schema, Migrations, DB-Hilfsfunktionen
  config/     # Konfigurationslogik (lädt config.yaml)
  tests/      # pytest-Tests
```

## Datenbank
- Schema: `db/schema.sql`
- Dateiidentität: `documents.hash` (SHA256)
- Mehrere Pfade pro Datei möglich: `document_paths`
- Volltextsuche: FTS5 Virtual Table `documents_fts`

## Konventionen
- FastAPI-Routen in `web/routes/`
- Jinja2-Templates in `web/templates/`
- Statische Dateien in `web/static/`
- Konfiguration über `config.yaml`, geladen via `config/settings.py`
- Tests mit pytest, Fixtures in `tests/conftest.py`
