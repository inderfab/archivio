# Archivio — Projekt-Status & Kontext (für Claude / Weiterarbeit)

> **Zweck dieser Datei:** Vollständiger Übergabe-Kontext, damit an Archivio in einer neuen
> Sitzung/einem neuen Account nahtlos weitergearbeitet werden kann. Liegt bewusst im Repo,
> damit sie account-übergreifend verfügbar ist. Ergänzt `CLAUDE.md` (Projektinstruktionen).
>
> **Stand: v3.0.11 (Server) · v3.1.0 (Helper) · 2026-07-10**

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
- Logs: **`~/Library/Application Support/Archivio/logs/server.log`** = der **echte** Scanner-/uvicorn-Log (Python-Logging, DATA_DIR-basiert — hier stehen `scanner.walker`-Zeilen wie Datei-Timeout/SIGKILL/RSS). Zusätzlich `~/Library/Logs/ArchivioServer.log` (Menubar/Watchdog). **Falle:** `~/.archivio/logs/server.log` ist ein **veralteter** Pfad (alte Config) — dort wird NICHT mehr geschrieben, Greps darauf sind irreführend leer. Die aktuelle Log-Datei zur Not per `lsof -p <server-pid> | grep '\.log'` verifizieren.
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
- Server: `scripts/build_server_app.sh` `_build_python()` lädt python-build-standalone (3.13), installiert `requirements.txt` + `rumps requests mcp`, cached in `dist/.python-*`. Bundle: `Contents/Resources/archivio-python-{arm64,x86_64}/` (bewusst nicht `Contents/Frameworks/` — codesign lehnt Verzeichnisse dort ohne gültige Framework-Struktur als "bundle format unrecognized" ab, siehe `scripts/sign_lib.sh`). Launcher wählt per `uname -m`.
- **Helper: seit 3.0.1 ebenfalls eingebettetes Python** (nur rumps+requests) → **kein Xcode/pip beim Nutzer nötig**. `helper/build.sh` baut ein minimales `dist/.python-helper-*` aus der gecachten Basis. Launcher bevorzugt eingebettetes Python, venv nur Fallback.
- Signierung: **nur** einzelne `.so`/`.dylib`/Binaries ad-hoc signieren, plus `codesign -s - --force --deep` fürs Helper-Bundle. Server-Bundle NICHT komplett signieren (macht Dateien immutable → späteres `rm -rf dist` scheitert; dann `mv dist dist-locked-… && mkdir dist`).

### Signierung & Notarisierung (Developer ID) — seit v3.0.18 / Helper 3.1.5

Beide Build-Skripte (`scripts/build_server_app.sh`, `helper/build.sh`) laden gemeinsam
`scripts/sign_lib.sh` (`sign_inner`, `sign_bundle`, `notarize_and_staple`). Ohne gesetzte
Env-Vars bauen sie **exakt wie vorher** ad-hoc-signiert, unsigniert/unnotarisiert — lokale
Entwicklung ohne Zertifikat bleibt unverändert möglich, nur eine Warnung pro Lauf.

Für einen echten, Gatekeeper-freien Build drei Env-Vars setzen (Identitäten via
`security find-identity -v -p codesigning` ermitteln):
```bash
export ARCHIVIO_SIGN_APP="Developer ID Application: ... (TEAMID)"
export ARCHIVIO_SIGN_INSTALLER="Developer ID Installer: ... (TEAMID)"
export ARCHIVIO_NOTARY_PROFILE="archivio-notary"   # einmalig per notarytool store-credentials
```
Danach `scripts/verify_release.sh <pfad-zu-.pkg-oder-.app>` zur Kontrolle (spctl, codesign/
pkgutil, stapler — Exit-Code 0 = alles grün).

**Harte Falle, die den kompletten Signierversuch blockiert hätte:** die eingebetteten
Python-Umgebungen lagen ursprünglich unter `Contents/Frameworks/archivio-python-{arch}/`.
`codesign` behandelt **jedes** Verzeichnis direkt unter `Contents/Frameworks/` als
vermeintliches Nested-Framework-Bundle und lehnt es ohne gültige Framework-Struktur
(`Versions/`, eigenes `Info.plist`) mit `"bundle format unrecognized, invalid, or unsuitable"`
ab — das verhindert jede Signatur des Gesamtbundles, unabhängig vom Inhalt. Fix: beide
Python-Umgebungen liegen jetzt unter `Contents/Resources/archivio-python-{arch}/` (gleiche
Verschachtelungstiefe, `app_path()` in `shared/menubar_bridge.py` musste dafür nicht
geändert werden). `Contents/Frameworks/` wird seither gar nicht mehr angelegt.

**Zweite harte Falle (iMac-Akzeptanztest, stiller Absturz ohne jede Fehlermeldung):**
`sign_inner` in `scripts/sign_lib.sh` signierte alle inneren Mach-O-Dateien (inkl. des
eingebetteten `python3`-Interpreters selbst) mit `--options runtime` (Hardened Runtime AN),
aber **ohne `--entitlements`**. Der Launcher ist ein Bash-Skript, das per `exec` direkt in
den `python3`-Prozess wechselt — Entitlements gelten pro Mach-O-Datei, nicht vererbt über
`exec` hinweg. Der tatsächlich laufende `python3`-Prozess hatte damit Hardened Runtime OHNE
die nötigen Ausnahmen (`disable-library-validation`, `allow-unsigned-executable-memory`) und
wurde vom Kernel beim ersten Versuch, ausführbaren Speicher zu allozieren
(numpy/cryptography/lxml/pymupdf u.a.), sofort per SIGKILL getötet — **noch bevor Python
irgendeine Ausgabe schreiben konnte**. Symptom: App startet laut Log ("Archivio Server vX.X.X
starting", Python-Version wird geprintet), dann nichts mehr — kein Traceback, kein Fehler,
kein Dock-Hüpfen. macOS zeigt ggf. "kann nicht geöffnet werden, weil es nicht reagiert" unter
Datenschutz & Sicherheit. Fix: `--entitlements config/entitlements.plist` auch beim inneren
Signieren (`sign_inner`) mitgeben, nicht nur beim äußeren Bundle (`sign_bundle`).
**Lehre:** bei allem, was per `exec` in einen eingebetteten Interpreter wechselt, müssen die
Entitlements auf der tatsächlich exec'ten Binary sitzen, nicht nur auf dem Launcher/Bundle.

**Erwartetes (kein Bug!) Verhalten: erste Installation auf einem neuen Mac dauert ~10 Minuten.**
Vorher (ad-hoc-signiert) prüfte Gatekeeper praktisch nichts → Installation quasi instant. Mit
echter Signatur + Notarisierung validiert macOS bei der `.pkg`-Installation die Codesignatur
**jeder einzelnen** Mach-O-Datei im Bundle gegen die Zertifikatskette — und die zwei
eingebetteten Python-Umgebungen (arm64 + x86_64) enthalten hunderte kompilierte `.so`/`.dylib`
(numpy, cryptography, lxml, pymupdf, tcl/tk …), jede einzeln signiert. Diese Tiefenprüfung ist
einmalig pro Mac und wird lokal gecacht — jede weitere Installation/jeder weitere Start auf
derselben Maschine ist wieder so schnell wie vorher. Bestätigt am 2026-07-27 auf einem
Intel-iMac: erste `.pkg`-Installation ~10 Min, zweite Installation direkt danach wieder <1 Min.

### Automatische Updates (Server) — Kapitel 2

`menubar/updater.py` prüft im Hintergrund (60s nach Start, danach alle 24h) über die
GitHub-Releases-API (`GITHUB_REPO = "inderfab/archivio"`), ob eine neuere Server-Version
existiert (`packaging.version.Version`-Vergleich statt String-`==`). Kein Sparkle, keine
eigene Kryptografie — Sicherheitsanker ist ausschließlich Apples eigene Signaturkette:
heruntergeladene `.pkg`-Dateien werden per `pkgutil --check-signature` (Signer-Typ
"Developer ID Installer" + Team-ID `2USYCLVGTM`) und zusätzlich `spctl -a -t install`
verifiziert, bevor der Installer angeboten wird. Jede fehlgeschlagene Prüfung löscht die
Datei sofort und bietet stattdessen die Download-Seite an. Installation ist bewusst nicht
still: `open <pkg>` öffnet den normalen System-Installer, kein Root-Install im Hintergrund
(kein `SMJobBless`, keine privilegierte Helper-Instanz).

Im Menü erscheint bei gefundenem Update ein zunächst verstecktes Item ganz oben
("⬆ Update auf vX.X.X verfügbar", via `rumps`-`insert_before`/`.hidden`), plus einmalige
Benachrichtigung pro Version (`~/Library/Application Support/Archivio/update_state.json`
verhindert Wiederholung bei jedem Tages-Check). Der bestehende manuelle Menüpunkt
"Auf Updates prüfen…" nutzt dieselbe Logik.

Das Postinstall-Skript (`scripts/build_server_app.sh`) deckt den Update-Fall bereits ab,
unverändert seit Kapitel 1: `launchctl bootout` → `pkill` alte Instanz → `launchctl
bootstrap`/`load` neu — die App startet ihr eigenes `.pkg` also auch **während sie noch
läuft** sauber neu.

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
- **Mail-Scan-Banner: „neu"/„unverändert" bleiben bei 0, obwohl der Fortschritt (X/Y Mails) korrekt hochzählt.** Ursache: `scan_mailbox()` (`scanner/mail_scanner.py`) schreibt `total`/`processed` laufend pro Mail in den `progress`-Dict, aber `new`/`skipped` erst als Rückgabewert — `_run_mail_scan` (`web/dashboard.py:1550-1557`) addiert diese erst in `_mail_scan["new"]`/`["skipped"]`, wenn `scan_mailbox()` für das GANZE Postfach zurückkehrt. Bei grossen Postfächern (z.B. 12'684 Mails) zeigt der Banner deshalb lange „0 neu, 0 unverändert". Fix: `new`/`skipped` analog zu `processed` direkt im `progress`-Dict laufend mitzählen statt erst am Ende.

---

## 15. Diagnose-Endpoints & nützliche Befehle

- `GET /api/status`, `GET /api/version`, `GET /api/scan/state`, `GET /api/debug/diagnostics`
- iMac-Log-Analyse (Nutzer per Copy-Paste): **`~/Library/Application Support/Archivio/logs/server.log`** (echter Scanner-Log!), `~/Library/Logs/ArchivioServer.log`. NICHT `~/.archivio/logs/` (veraltet, leer).
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

---

## 18. Rubrica-Integration (Adressvorschläge aus Mail-Signaturen)

**Rubrica** ist eine zweite, separate App (CardDAV-Adressbuch, läuft ebenfalls auf dem iMac). Sie
leitet aus Mail-Signaturen Kontaktvorschläge ab (Regel u.a.: min. 2 Korrespondenzen mit
gleichem Sender/Empfänger), sortiert nach Projekt, zur manuellen Review. Archivio schneidet die
Signatur beim Scannen bewusst ab (`_strip_signature`, `scanner/mail_scanner.py`), um Rauschen in
der Volltextsuche zu vermeiden — Rubrica braucht aber genau den vollen Text.

- **Zweite, separate SQLite-Datei `rubrica.db`** (Default: gleiches Verzeichnis wie `archivio.db`,
  Pfad über `rubrica.db_path` in `config.yaml` überschreibbar). Neues Modul `db/rubrica.py`
  spiegelt `db/connection.py` (WAL, `timeout=30`, idempotentes `CREATE TABLE/INDEX IF NOT EXISTS`
  bei jedem Connect — kein separates Migrationssystem für die eine Tabelle nötig).
- **Tabelle `signatur_quelle`** — Spalten: `message_id` (UNIQUE, Dedup-Schlüssel), `absender`
  (+ `absender_email` rein), `empfaenger`, `cc`, `postfach`, `projekt` (Name, denormalisiert —
  Rubrica braucht keinen Zugriff auf `archivio.db`), `betreff`, `text` (voller Mailtext INKL.
  Signatur), `datum`, `status`, `status_updated_at`, `created_at`.
- **Schreib-Vertrag — strikt eingehalten, da beide Apps in dieselbe Datei schreiben (WAL nötig):**
  - **Archivio** schreibt AUSSCHLIESSLICH `INSERT OR IGNORE` (nie UPDATE/DELETE), immer mit
    `status='pending'`.
  - **Rubrica** schreibt AUSSCHLIESSLICH `status` + `status_updated_at` zurück (`'pending'` →
    `'processed'`/`'rejected'`), fasst keine andere Spalte an. So kann Rubrica einfach nach
    `status='pending'` pollen statt jedes Mal den ganzen Bestand zu prüfen, und abgelehnte Zeilen
    werden nie erneut vorgeschlagen.
- **Hook:** `scanner/mail_scanner.py::save_mail_to_db` ruft nach dem erfolgreichen
  Dokument-Insert `db.rubrica.save_signature_source(record, project_name, mailbox_name)` — eigenes
  try/except, ein Rubrica-DB-Fehler darf den Mail-Scan nie abbrechen. `record["raw_text"]`
  (voller Text, unbereinigt) wurde in `build_email_record` schon immer berechnet, nur nie
  persistiert. `project_name` wird einmal pro Postfach in `scan_mailbox` aufgelöst (nicht pro
  Mail). No-op wenn `rubrica.enabled` (config.yaml) nicht `true` ist — einzige Stelle, die das
  Flag prüft.
- **Config (`config.yaml`, NICHT automatisch aus `config.yaml.example` übernommen bei
  bestehenden Installationen — manuell ergänzen):**
  ```yaml
  rubrica:
    enabled: true
    db_path: ""   # leer = Standard neben archivio.db
  ```
- **Backfill-Altbestand:** ein normaler inkrementeller Scan überspringt bekannte Mails schon beim
  Header-Fetch (`mail_exists()`-Check) — der volle Body wird für längst indexierte Mails nie
  erneut geholt. Für den Bestand: `scripts/backfill_rubrica.py` (einmalig, manuell im Terminal).
  Geht unabhängig vom normalen Scan-Pfad nochmal komplett über alle aktiven Postfächer, Dedup
  gegen `signatur_quelle.message_id` (nicht gegen `archivio.db`) — sicher wiederholt ausführbar.
  - **Dev-Mac:** `.venv/bin/python scripts/backfill_rubrica.py`
  - **iMac (installierte App, seit Build mit `scripts/`-Bundling):**
    ```bash
    ARCHIVIO_DATA_DIR="$HOME/Library/Application Support/Archivio" \
      "/Applications/Archivio Server.app/Contents/Resources/archivio-python-x86_64/bin/python3" \
      "/Applications/Archivio Server.app/Contents/Resources/scripts/backfill_rubrica.py"
    ```
    (`archivio-python-x86_64` beim iMac — Apple Silicon nutzt `-arm64`.) Nur
    `scripts/backfill_rubrica.py` wird ins Bundle kopiert (`Contents/Resources/scripts/`), nicht
    der ganze `scripts/`-Ordner (der auch Build-/Dev-Tooling enthält).
- Tests: `tests/test_rubrica.py` (Disabled-No-op, Insert, Dedup, End-to-End: bereinigter Text in
  `archivio.db` vs. voller Text in `rubrica.db`).
