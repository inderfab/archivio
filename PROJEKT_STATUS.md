# Archivio — Projekt-Status & Kontext (für Claude / Weiterarbeit)

> **Zweck dieser Datei:** Vollständiger Übergabe-Kontext, damit an Archivio in einer neuen
> Sitzung/einem neuen Account nahtlos weitergearbeitet werden kann. Liegt bewusst im Repo,
> damit sie account-übergreifend verfügbar ist. Ergänzt `CLAUDE.md` (Projektinstruktionen).
>
> **Stand: v3.0.5 (Server) · v3.1.0 (Helper) · 2026-07-08**

---

## 1. Was ist Archivio

Vollständig **lokale** Dokumenten- und Mail-Suchplattform für das Architekturbüro (Peter Kunz
Architektur / Strut Architekten). Keine Cloud. Läuft auf einem iMac im Büronetz, indexiert
Dateien vom NAS und Mails per IMAP.

- **Stack:** Python 3.13 (eingebettet), FastAPI, SQLite + FTS5, HTMX + Jinja2, rumps (Menubar-App), Ollama (Embeddings `nomic-embed-text` + LLM `llama3.2:3b`)
- **Repo:** https://github.com/inderfab/archivio (GitHub-User: `inderfab`)
- **Website:** https://bauchat.ch (GitHub Pages aus `docs/`, Custom Domain)
- **Dateiidentität:** SHA256-Hash (nicht Pfad) → Duplikate/Verschiebungen werden erkannt. **Wichtig:** dadurch kann *ein* Dokument mehrere Pfade in verschiedenen Projekten haben.

---

## 2. Infrastruktur

**iMac — Produktions-Server (User `pas`, Intel):**
- LAN: `http://windows.local:8000` (Gerätename ist „Windows"). Kann per Bonjour-LocalHostname zu `archivio.local` gemacht werden, oder per Router-DNS zu `archivio:8000`.
- App: `/Applications/Archivio Server.app/` (per `.pkg` installiert)
- DATA_DIR: `~/Library/Application Support/Archivio/` (DB, config.yaml, Helper-ZIP)
- Logs: `~/Library/Logs/ArchivioServer.log` (Menubar/Watchdog) **und** `~/.archivio/logs/server.log` (uvicorn) + `~/.archivio/logs/launchd-server.log`
- DB: `~/Library/Application Support/Archivio/archivio.db`
- Ollama läuft dort (Port 11434)
- **Autostart:** LaunchAgent `~/Library/LaunchAgents/io.archivio.server.plist` (siehe §5)

**Dev-Mac (User `fi`, Apple Silicon) — hier wird entwickelt & gebaut:**
- Pfad: `/Users/fi/archivio`
- venv: `.venv` (Python 3.9 — nur für Tests/lokalen Lauf; **psutil ggf. nachinstallieren**: `pip install psutil`, wird für walker-Tests gebraucht)
- Lokaler Lauf: `.venv/bin/uvicorn web.main:app --reload --port 8000`
- Build: `bash scripts/build_server_app.sh` (baut Server **und** Helper)
- Der Bash-Tool-Zugriff von Claude läuft auf DIESEM Mac — **nicht** auf dem iMac. iMac-Diagnose nur über den Nutzer (Copy-Paste von Terminal-Befehlen).

**Mac Studio (Mitarbeiter, Apple Silicon):** Nur Helper installiert.

**NAS (config.yaml, gitignored):**
- Projekte: `/Volumes/Groups/Peter Kunz Architektur/Projekte`
- Office: `/Volumes/Groups/Peter Kunz Architektur/Office`
- Geteilt: `/Volumes/geteilte Projekte`

**Mail:** IMAP hostpoint, Konto `projekte@strut.ch`. **Passwort steht in `config.yaml` (gitignored) — DARF NIE nach GitHub.**

---

## 3. Versionierung (WICHTIG — entkoppelt)

- **`VERSION`** (Repo-Root) = **Server-Version**, zählt bei jedem Release hoch.
- **`helper/VERSION`** = **Helper-Version**, bleibt stabil (aktuell 3.0.1), wird **nur bei echten Helper-Änderungen** hochgezählt.
- Der Server bündelt `HELPER_VERSION` (Datei im Bundle) und meldet sie über `GET /api/version` → `{"version": <server>, "helper_version": <helper>}`.
- Der **Helper vergleicht sein Update gegen `helper_version`**, nicht gegen die Server-Version → ein Server-Update löst **kein** Helper-Update aus.
- Merke: „Server hochzählen, immer auf Helper 3.0.x verweisen."

---

## 4. Build- & Release-Prozess

```bash
# 1. Branch für Änderungen (NICHT direkt auf main entwickeln)
git checkout -b fix/xyz

# 2. Server-Version setzen
printf '3.0.5' > VERSION            # helper/VERSION nur bei Helper-Änderung anfassen

# 3. Tests
.venv/bin/python -m pytest tests/ -q

# 4. Bauen (Server-PKG/ZIP + Helper-ZIP; eingebettetes Python wird gecacht)
bash scripts/build_server_app.sh    # -> dist/archivio-server-3.0.5.pkg/.zip + dist/archivio-helper-3.0.1.zip

# 5. Commit + (nach Test durch Nutzer) mergen
git add -A && git commit -m "..."
git checkout main && git merge --no-ff fix/xyz -m "Merge: v3.0.5 — ..."

# 6. Sauber neu bauen + Release
rm -f dist/*.pkg dist/*.zip; rm -rf dist/*.app
bash scripts/build_server_app.sh
git push origin main
gh release create v3.0.5 dist/archivio-server-3.0.5.pkg dist/archivio-server-3.0.5.zip \
  --title "v3.0.5 — ..." --notes-file /tmp/relnotes.md

# 7. Aufräumen: alten Release + Tag löschen (Nutzer will i.d.R. nur das neueste)
gh release delete v3.0.4 --yes --cleanup-tag
git branch -d fix/xyz
```

- **Workflow-Konvention des Nutzers:** Änderungen im Branch → Test-PKG bauen → Nutzer testet auf dem iMac → dann erst mergen/releasen. Nach dem Release nur **das neueste** Release behalten (alte löschen).
- Commit-Messages/Notes auf Deutsch. Co-Author-Zeile ans Ende von Commits.
- `dist/` ist **gitignored** (Build-Artefakte). Nach Version-Bump erzeugt der Build viele Alt-Artefakte → vor Release `rm -f dist/*.pkg dist/*.zip; rm -rf dist/*.app`.

### Eingebettetes Python
- Server: `scripts/build_server_app.sh` `_build_python()` lädt python-build-standalone (3.13), installiert `requirements.txt` + `rumps requests`, cached in `dist/.python-*`. Bundle: `Contents/Frameworks/archivio-python-{arm64,x86_64}/`. Launcher wählt per `uname -m`.
- **Helper: seit 3.0.1 ebenfalls eingebettetes Python** (nur rumps+requests) → **kein Xcode/pip beim Nutzer nötig**. `helper/build.sh` baut ein minimales `dist/.python-helper-*` aus der gecachten Basis. Launcher bevorzugt eingebettetes Python, venv nur Fallback.
- Signierung: **nur** einzelne `.so`/`.dylib`/Binaries ad-hoc signieren, plus `codesign -s - --force --deep` fürs Helper-Bundle. Server-Bundle NICHT komplett signieren (macht Dateien immutable → späteres `rm -rf dist` scheitert; dann `mv dist dist-locked-… && mkdir dist`).

---

## 5. Zuverlässigkeit / Auto-Restart (zwei Ebenen)

**Ebene 1 — In-App-Watchdog (`menubar/server_app.py`, `_server_memory_watchdog`):** überwacht den uvicorn-Prozess alle 15s. (a) Prozess tot → sofort neu; (b) hängt (4× kein HTTP-200) → neu; (c) RSS > 20 GB → kontrollierter Neustart. `_restart_server(resume_projects, resume_mail)` setzt den **richtigen** Scan-Typ fort (nur Mail-Scan → `/dashboard/mail/scan`; Projekt-Scan → `/api/scan/all`). Merkt sich den letzten Stand für Resume nach Crash.

**Ebene 2 — launchd LaunchAgent (`io.archivio.server`):** hält die **ganze App** am Leben.
- **`KeepAlive = true`** (seit 3.0.4!) + `RunAtLoad=true` + `ThrottleInterval=30`.
- **Historie/Falle:** Vorher war `KeepAlive = SuccessfulExit:false` → nach einem **sauberen Exit 0** (macOS Logout/Ruhezustand/Update übers Wochenende) startete launchd **nicht** neu → Server lag tagelang tot. Deshalb jetzt `true`.
- „Beenden" im Menü macht `launchctl bootout` (sonst würde KeepAlive sofort neu starten).
- Postinstall installiert/lädt den Agent (`launchctl bootstrap gui/$UID`), entfernt altes Login-Item, killt vorher laufende manuelle Instanz.
- **Diagnose auf iMac:** `launchctl print "gui/$(id -u)/io.archivio.server" | grep state` → muss `running` sein.

---

## 6. Scanner (`scanner/walker.py`) — Kernwissen & Fallen

- **Streaming `os.walk`**, `multiprocessing.Pool` (spawn), `num_workers` aus config (iMac: 1).
- **Skip-Pfad im Hauptprozess** (Performance): unveränderte Dateien (Pfad+Größe+mtime, status in ok/listed/error/unsupported) werden per `stat()` + indexierter SELECT übersprungen — **ohne** Worker/IPC. `skip_conn` ist eine reine Lese-Verbindung (nur SELECTs → keine Transaktion → kein WAL-Snapshot-Problem).
- **`_process_file`:** Fast-Path → List-Only (Bilder/Video/3D/Disk-Images, `_LIST_ONLY_EXTENSIONS`) → **unbekannte Formate = auch list-only** (per Dateiname suchbar) → sonst SHA256 + Extraktion. `supported = _supported_extensions()` MUSS lokal geholt werden (war mal ein NameError-Bug).
- **Müll-Filter `_is_junk_file`:** versteckte Dateien, `~$…`, `…~`, `Thumbs.db`, `desktop.ini`, `.DS_Store`, `.lock/.tmp/.part/.crdownload/.swp/.bak`.
- **`_worker_status(pid)` (seit 3.0.3):** nur Prozesse, deren Parent DIESER Prozess ist, gelten als Worker. RSS-Zählung und alle SIGKILLs überspringen fremde PIDs. **Grund:** eine wiederverwendete tote Worker-PID zählte sonst den RSS eines Fremdprozesses (z. B. Ollama 12 GB) → falsche „12.8 GB"-Messung → jeder Worker sofort gekillt → Scan kroch 18h ohne Fortschritt.
- **Stall-Abbruch (seit 3.0.3):** nach `_MAX_CONSECUTIVE_STALLS = 8` Timeouts/Speicher-Kills in Folge bricht der Scan mit „NAS-Verbindung prüfen" ab (statt stundenlang bei hängendem NAS zu kriechen).
- **RAM-Limits:** `_MAX_WORKER_RSS` = 20% RAM (64 GB → 12.8 GB), Datei-Timeout 120s (Nicht-PDF), da SIGALRM bei NAS-I/O nicht durchkommt.
- **D-State-Falle:** Worker in unterbrechbarem NAS-I/O sind nicht sofort killbar (OS-Limit). Hängt das NAS, hilft nur der Stall-Abbruch + NAS neu verbinden.
- **FTS-Automerge:** während des Scans `automerge=0`; das teure `optimize` läuft NACH dem Scan koordiniert über `_run_fts_optimize` (hält den Scan-Lock → nie parallel zu Inserts, sonst „database is locked" → verlorene Dokumente).

---

## 7. Embedding (`web/dashboard.py`)

- Läuft **nach** dem Scan, nicht im Worker. `_run_post_scan_embedding` wartet, bis der GANZE Scan-Batch (inkl. Warteschlange + Mail) fertig ist (`_any_scan_active`).
- **`_embedding_ram_ok()` misst PROZESS-RSS (< 15 GB), nicht system-weites RAM%** (seit 3.0.1). Grund: der launchd/Watchdog killt bei 20 GB Prozess-RSS — system-RAM% (80%) griff auf großen Maschinen nie rechtzeitig → Embedding trieb den Server in einen Neustart-Loop.
- `_resume_embeddings_on_startup` holt beim Start offene Chunks nach. Große Scans erzeugen riesige Chunk-Rückstände (>70k) → mit korrekter Drossel unkritisch.

---

## 8. Suche (`web/main.py`)

- **Scope:** `search_in` (`docs,folders,filenames`, + `plans`). `docs` → `chunks_fts` (Fallback LIKE); `filenames` → `documents_fts` mit `filename:term*`; `folders` → `_search_folders`.
- **`_make_fts_query`:** Split auf Space UND Punkt; deutsche Komposita (Nachbarwörter zusammengeklebt vor/rück).
- **`_search_folders` (Filter-Fix seit 3.0.4):** filtert bei Projektauswahl nach **Projektpfad** (`dp.path LIKE projektpfad/%`), NICHT nur `project_id`. Grund: durch Hash-Dedup hat ein Dokument mehrere Pfade in verschiedenen Projekten → `project_id`-Filter zeigte sonst fremde Projektordner (z. B. HB-Therm/Skyframe bei Auswahl „200 Keller"). Vorfilterung in SQL (`LIKE %wort%`), `folder.exists()` (NAS-Stat) nur für die wenigen Treffer.
- **KI-Suche:** `/search/ai` (~1s, keyword+vector, max 12 Quellen) → `/search/ai/answer` (~30s LLM). Toggle „KI-Suche".
- **Such-Dropdown:** „Mail" liegt in der Gruppe „Kategorien".

---

## 9. Mail-Integration

- Mails = Dokumente mit `source_type='email'`, Metadaten in `mails` (mit `mailbox_name`). **`documents.project_id` ist NOT NULL** → Mails werden nur gespeichert, wenn das Postfach einem Projekt zugewiesen ist; unassigned-aktive Postfächer werden beim Scan übersprungen.
- **Postfach = wie Projekt behandeln** (seit 3.0.2): gleiche Zeilendarstellung, per-Postfach „Jetzt/Neu scannen" (`/mail/scan-one`), stale-first (`ORDER BY last_scanned_at ASC`), großer Scan-Banner zeigt aktuelles Postfach, Datum+Uhrzeit (`fmt_datetime`).
- **Löschen:**
  - Projekt löschen → löscht auch Mails der verknüpften Postfächer **per `mailbox_name`** (nicht nur `project_id` — deckt umgehängte Postfächer ab). CASCADE + FTS-Trigger.
  - Postfach deaktivieren → Dialog „nur deaktivieren / Mails löschen" (`_dashboard_mail_confirm_remove.html`, Endpoints `/mail/deactivate`, `/mail/delete`).
- **Projekt-Scan scannt verknüpftes Postfach mit** (`_run_scan(scan_mail=True)` bei Einzel-Scan; bei „Alle scannen" `scan_mail=False`, globaler Mail-Scan übernimmt → kein Doppelscan).

---

## 10. Dashboard-Darstellung

- **Aktive** Objekte (Projekte + Postfächer): volle 3-Zeilen-Darstellung (Name, „X Dok./Mails · Zuletzt gescannt: TT.MM.JJJJ HH:MM", Pfad/Zuordnung) + altersabhängiges Badge (`_scan_freshness`: grün ≤2 Tage, sonst amber „vor X Tg.") + „Jetzt/Neu scannen"-Button (**immer** verfügbar, über eine `scan-cell` mit stabiler ID → saubere HTMX-Swaps, kein Doppel-Badge).
- **Nicht-aktive** Objekte: kompakt (nur Toggle + Name, eine Zeile).
- **Scan-All** (`/api/scan/all`): sequenziell (ein `_scan_lock`), **stale-first** (`ORDER BY last_scanned_at ASC`) → konvergiert über Neustarts. Voller `_scans`-Eintrag (Name/Pfad/Zähler). JS lädt Projektliste sofort neu, damit Zeilen ihr pollendes Badge bekommen.
- Status `_scans`/`_mail_scan` sind **In-Memory** → nach Neustart weg; deshalb wird der durable Zustand aus `projects.last_scanned_at` gezogen.

---

## 11. Datenbank

- Schema: `db/schema.sql`. Migrationen `db/migrations.py` laufen beim Start (`init_schema`).
- **Migrationen 001–009.** Wichtige aus dieser Historie:
  - **007:** `extraction_status` erlaubt zusätzlich `'listed'`. **KRITISCH:** vorher fehlte `'listed'` in der CHECK-Constraint → jeder Bild-/List-Only-Insert warf IntegrityError → ganze Transaktion (Dokument+Pfad) zurückgerollt → Bilder landeten NIE in der DB. (SQLite kann CHECK nicht per ALTER ändern → `writable_schema`-Patch.)
  - **008:** `documents_fts_doc_delete`-Trigger (AFTER DELETE ON documents) → sonst verwaiste FTS-Dateinamen-Treffer bei Dokumenten ohne `document_content`.
  - **009:** `projects.last_scanned_at`.
- **`queries.upsert_path` (Fix):** hängt Pfad per `ON CONFLICT(path) DO UPDATE` auf das aktuelle Dokument um + räumt verwaiste Alt-Version auf. Vorher (`INSERT OR IGNORE`) blieb der Pfad bei geänderten Dateien auf der alten Version → neues Dokument verwaist.
- **Beim Projekt-Löschen:** `mail_scan_config` hat kein CASCADE → separat löschen. Deletion großer Projekte im Hintergrund-Thread (`_delete_project_bg`) mit Polling.

---

## 12. Tests

- `tests/` mit pytest. **`conftest.py`:** setzt `ARCHIVIO_DATA_DIR` (wird an spawn-Worker vererbt!) + eigene config.yaml, Datenverzeichnis GETRENNT von den gescannten Dateien. **Falle:** ohne das schreiben spawn-Worker in die echte Repo-`archivio.db` (Monkeypatch überquert Prozessgrenze nicht) → grüne, aber wertlose Tests.
- `test_walker.py`, `test_mail_delete.py`, `test_hasher.py` grün. `test_search_recall.py` braucht Ollama + echte DB (`ORDER BY RANDOM()`) → flaky/skip, ist KEINE Regression.
- Dev-venv braucht `psutil` für walker-Tests (`pip install psutil`).

---

## 13. Website bauchat.ch

- GitHub Pages aus `main:/docs` (statisches HTML: `index.html`, `docs.html`, `img/`, `CNAME`).
- **`docs/.nojekyll`** vorhanden (seit v3.0.x) → Pages liefert statisch aus, **kein Jekyll-Build** mehr → keine flaky „page build failed"-Mails bei jedem Push.
- Custom Domain `bauchat.ch` (A-Records bei Hostpoint → GitHub Pages).

---

## 14. Offene Punkte / Beobachten

- **RAM-Wachstum des Servers über Laufzeit** (schleichend, evtl. Embedding-Verarbeitung). Mit Watchdog+launchd unkritisch, aber Ursache nie final geprofiled. Könnte man mit tracemalloc auf dem iMac untersuchen.
- **Adresse `archivio:8000`** statt `windows.local:8000`: geht über (a) Bonjour LocalHostname `archivio` → `archivio.local:8000`, oder (b) Router-DNS + feste IP → `archivio:8000`. Kein App-Change nötig; ggf. LaunchAgent, der `scutil --set LocalHostName archivio` setzt.
- **Alte Müll-Dokumente** (Thumbs.db etc. aus früheren Scans) bleiben in der DB, bis manuell bereinigt.
- **Nacht-Scan (Scheduler 22:00, `web/main.py:_scheduler_loop`)** postet `/api/scan/all`. Bei sehr großen Beständen + Neustarts konvergiert es über mehrere Nächte (stale-first).

---

## 15. Diagnose-Endpoints & nützliche Befehle

- `GET /api/status`, `GET /api/version`, `GET /api/scan/state`, `GET /api/debug/diagnostics`
- iMac-Log-Analyse (Nutzer per Copy-Paste): `~/Library/Logs/ArchivioServer.log`, `~/.archivio/logs/server.log`
- Worker-RSS auf iMac: `ps aux | grep archivio-python`
- launchd-Status: `launchctl print "gui/$(id -u)/io.archivio.server"`

---

## 16. Sicherheits-Randbedingung (IMMER beachten)

Die lokal gelesenen **Mail-Zugangsdaten (bauchat/strut, `config.yaml`) dürfen NIE nach GitHub**. `config.yaml` ist gitignored, das Datenverzeichnis liegt außerhalb des Repos, nur Code wird committet. `*.db` ist gitignored.

---

## 17. MCP-Server (Claude Desktop) — seit v3.0.5 / Helper 3.1.0

Claude Desktop kann Archivio als lokales **MCP-Tool** nutzen (vollständig lokal, kein Cloud-Dienst). Der MCP-Server ist **in den Helper integriert** (nicht separat verteilt) und läuft mit dessen eingebettetem Python.

- **Server-Endpunkte (`web/api.py`, alle read-only JSON):** `GET /api/mcp/search` (nutzt `_build_filters`+`_search` aus `web/main.py`, entfernt `<mark>`-Tags), `GET /api/mcp/semantic-search` (nutzt `_ai_vector_search`, liefert Chunk-`content`; ohne Ollama sauberes `ollama_missing`), `GET /api/mcp/document?document_id=` (Volltext + bei Mails Absender/Betreff/… für `read_document`).
- **MCP-Server (`helper/archivio_mcp.py`, stdio, FastMCP):** 5 Tools — `search`, `semantic_search`, `read_document` (Text in den Chat laden → umschreiben/zusammenfassen), `open_file` + `reveal_file` (extern öffnen / im Finder zeigen via Helper-HTTP `localhost:44380`). Suchergebnisse zeigen `[ID nnn]` → an `read_document` übergeben.
- **Server-URL-Auflösung:** `_server_url()` fragt ZUERST den laufenden Helper (`GET localhost:44380/config` → im Menü gesetzte URL, z.B. `windows.local:8000`), dann eigene `config.json`, dann `localhost:8000`. So kein falscher Server durch veraltete gebündelte config.
- **Auto-Registrierung:** `_ensure_mcp_registered()` (in `archivio_helper.py`, beim Start) trägt idempotent einen `archivio`-Eintrag in `~/Library/Application Support/Claude/claude_desktop_config.json` ein (`command`=`sys.executable`=eingebettetes Python, `args`=archivio_mcp.py), ohne andere `mcpServers` anzutasten. Danach Notification „Claude Desktop neu starten".
- **Helper-HTTP `/config`-Endpoint** neu; `_cors_headers(code, body=None)` kann jetzt JSON-Body senden. **404-Fix:** `/open`+`/reveal` senden die HTTP-Antwort VOR `rumps.notification` (die aus dem HTTP-Thread eine Exception werfen kann → vorher leere Antwort statt sauberem 404).
- **Nach Claude-Desktop-**oder**-`archivio_mcp.py`-Änderung: Claude Desktop neu starten** (lädt den stdio-Subprozess neu). Nach Helper-Änderung (`archivio_helper.py`): Helper-App neu starten.
- **`mcp`-Paket** ist im eingebetteten Helper-Python (arm64+x86_64) via `helper/build.sh` (Cache-Stamp `rumps+requests+mcp`). Tests: `tests/test_mcp_api.py`.
