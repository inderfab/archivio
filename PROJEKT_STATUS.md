# Archivio — Projekt-Status & Kontext (für Claude / Weiterarbeit)

> **Zweck dieser Datei:** Vollständiger Übergabe-Kontext, damit an Archivio in einer neuen
> Sitzung/einem neuen Account nahtlos weitergearbeitet werden kann. Liegt bewusst im Repo,
> damit sie account-übergreifend verfügbar ist. Ergänzt `CLAUDE.md` (Projektinstruktionen).
>
> **Stand: v3.4.0 (Server) · v3.1.35 (Helper) · 2026-09-23**

---

## 1. Was ist Archivio

Vollständig **lokale** Dokumenten- und Mail-Suchplattform für ein Architekturbüro. Keine
Cloud. Läuft auf einem Mac im Büronetz, indexiert Dateien vom NAS und Mails per IMAP.

- **Stack:** Python 3.13 (eingebettet), FastAPI, SQLite + FTS5, HTMX + Jinja2, rumps (Menubar-App), Ollama (Embeddings `nomic-embed-text` + LLM `llama3.2:3b`)
- **Repo:** https://github.com/inderfab/archivio (GitHub-User: `inderfab`)
- **Website:** https://bauchat.ch (GitHub Pages aus `docs/`, Custom Domain)
- **Dateiidentität:** SHA256-Hash (nicht Pfad) → Duplikate/Verschiebungen werden erkannt. **Wichtig:** dadurch kann *ein* Dokument mehrere Pfade in verschiedenen Projekten haben.

---

## 2. Infrastruktur

**iMac — Produktions-Server (Intel):**
- LAN: `http://windows.local:8000` (Gerätename ist „Windows"). Kann per Bonjour-LocalHostname zu `archivio.local` gemacht werden, oder per Router-DNS zu `archivio:8000`.
- App: `/Applications/Archivio Server.app/` (per `.pkg` installiert)
- DATA_DIR: `~/Library/Application Support/Archivio/` (DB, config.yaml, Helper-ZIP)
- Logs: **`~/Library/Application Support/Archivio/logs/server.log`** = der **echte** Scanner-/uvicorn-Log (Python-Logging, DATA_DIR-basiert — hier stehen `scanner.walker`-Zeilen wie Datei-Timeout/SIGKILL/RSS). Zusätzlich `~/Library/Logs/ArchivioServer.log` (Menubar/Watchdog). **Falle:** `~/.archivio/logs/server.log` ist ein **veralteter** Pfad (alte Config) — dort wird NICHT mehr geschrieben, Greps darauf sind irreführend leer. Die aktuelle Log-Datei zur Not per `lsof -p <server-pid> | grep '\.log'` verifizieren.
- DB: `~/Library/Application Support/Archivio/archivio.db`
- Ollama läuft dort (Port 11434)
- **Autostart:** LaunchAgent `~/Library/LaunchAgents/io.archivio.server.plist` (siehe §5)

**Dev-Mac (Apple Silicon) — hier wird entwickelt & gebaut:**
- Pfad: lokaler Checkout des Repos (Repo-Wurzel)
- venv: `.venv` (Python 3.14 mit SQLite 3.53.x = dieselbe SQLite-Version wie das Bundle, siehe §12 — nur für Tests/lokalen Lauf)
- Lokaler Lauf: `.venv/bin/uvicorn web.main:app --reload --port 8000`
- Build: `bash scripts/build_server_app.sh` (baut Server **und** Helper)
- Der Bash-Tool-Zugriff von Claude läuft auf DIESEM Mac — **nicht** auf dem iMac. iMac-Diagnose nur über den Nutzer (Copy-Paste von Terminal-Befehlen).

**Mac Studio (Mitarbeiter, Apple Silicon):** Nur Helper installiert.

**NAS (config.yaml, gitignored):**
- Projekte: `/Volumes/Groups/<Büro>/Projekte`
- Office: `/Volumes/Groups/<Büro>/Office`
- Geteilt: `/Volumes/<geteilter Ordner>`

**Mail:** IMAP, Konto siehe `config.yaml`. **Passwort steht in `config.yaml` (gitignored) — DARF NIE nach GitHub.**

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

**Ebene 3 — Einmal-Start-Sperre (`bridge.acquire_single_instance_lock`, seit 3.3.0):** `fcntl.flock` auf `~/.archivio/archivio-server.lock`, gesetzt in `menubar/server_app.py::__main__` **vor** allem anderen.
- **Warum:** Der Server konnte über zwei Wege gleichzeitig starten — den LaunchAgent **und** ein Anmeldeobjekt aus älteren Installationen. Der Postinstall versucht das Login-Item zu löschen, scheitert dabei aber still (Automation-Berechtigung fehlt im Installer-Kontext). Dann startete jeder Supervisor seinen eigenen uvicorn und `_start_server()` → `_kill_port_8000()` schoss den jeweils anderen ab. Im Log auf dem iMac sichtbar als zwei „uvicorn gestartet" im Abstand von 37 ms plus ein Watchdog-Neustart 15 s später — ~30 s Gerangel nach jeder Anmeldung.
- **KRITISCH — die zweite Instanz beendet sich NICHT,** sondern wartet in Bereitschaft (`while not acquire…: sleep(30)`) und übernimmt, wenn der aktive Supervisor wegfällt. Ein `sys.exit(0)` wäre hier falsch: mit `KeepAlive=true` (das bleiben muss, siehe oben) würde launchd alle 30 s eine neue Instanz starten, die sich sofort wieder beendet.
- Das Fehlschlagen von `flock` **muss** das Handle schliessen — sonst leckt die 30-s-Warteschleife Dateideskriptoren (Test: `tests/test_single_instance_lock.py`).
- Port 44380 taugt **nicht** als Sperre: auf einem Mac mit Server **und** Helper binden ihn beide, einer verliert ohnehin still.
- `_kill_port_8000()` benutzt `-sTCP:LISTEN` (sonst trifft es auch ausgehende Verbindungen zu einem Archivio auf einem anderen Mac) und lässt einen **gesunden fremden Server** in Ruhe; `_start_server()` übernimmt ihn dann, statt einen eigenen zu starten.
- **Logs:** `ArchivioServer.log`/`ArchivioHelper.log` rotieren (5 MB × 3) und laufen auf INFO, urllib3/zeroconf auf WARNING. Vorher DEBUG ohne Rotation → 321 MB bzw. 24 MB, ~2,7 MB/Tag.
- „Beenden" im Menü macht `launchctl bootout` (sonst würde KeepAlive sofort neu starten).
- Postinstall installiert/lädt den Agent (`launchctl bootstrap gui/$UID`), entfernt altes Login-Item, killt vorher laufende manuelle Instanz.
- **Diagnose auf iMac:** `launchctl print "gui/$(id -u)/io.archivio.server" | grep state` → muss `running` sein.

---

### Startseite vor dem Serverstart (seit v3.4.3)

`bridge.startseite_an(8000, log)` / `startseite_aus(log)` — ein winziger HTTP-Dienst belegt Port 8000, **bevor** uvicorn läuft, und antwortet überall mit „Archivio startet" (HTTP 503, Selbstaktualisierung alle 3 s).

- **Warum zusätzlich zur Warteseite oben:** die deckt nur die Migrationen *innerhalb* der App ab. Zwischen „Menüleisten-App gestartet" und „uvicorn nimmt Verbindungen an" liegen nach einer Neuinstallation bis zu einer Minute (Datenverzeichnis, Python-Bundle, schwere Importe) — dort gab es gar keine Antwort.
- Eingeschaltet in `_boot()` und in `_restart_server()` (nach `_stop_server()`); ausgeschaltet als **erste** Anweisung in `_start_server()`, vor `_kill_port_8000()`. Reihenfolge ist zwingend: sonst hält die Startseite den Port, den uvicorn gleich braucht.
- Liegt in `shared/menubar_bridge.py`, damit sie testbar ist (`tests/test_startseite.py`) — `menubar/server_app.py` lässt sich wegen rumps nicht importieren.
- Ist der Port schon belegt, wird still übersprungen statt den App-Start zu verhindern.

---

### Startvorbereitung & Warteseite (seit v3.4.0)

Schema + Migrationen laufen **im Hintergrund** (`web/main.py::_startvorbereitung`), nicht mehr blockierend im `lifespan`. Solange sie laufen, liefert eine Middleware allen Aufrufen eine Warteseite („Archivio wird vorbereitet", Selbstaktualisierung alle 4 s, HTTP 503).

- **Warum das kein Schönheitsfix ist:** vorher nahm uvicorn erst nach den Migrationen Verbindungen an. Die v3.4.0-Migration braucht auf 240'000 Dokumenten ~3 min — in der Zeit sah der Nutzer nur „Verbindung fehlgeschlagen". **Schlimmer:** der Watchdog prüft alle 15 s `/api/status` und startet nach vier Fehlversuchen (60 s) neu. Eine Migration über 60 s wäre also mitten drin abgeschossen und von vorn begonnen worden — bei `_m026` (FTS-Neuaufbau, wird erst nach Abschluss in `_migrations` eingetragen) potenziell endlos.
- **`/api/status` wird durchgelassen und OHNE Datenbankzugriff beantwortet** (`{"server": true, "vorbereitung": true, "seit_s": n}`). Eine Abfrage gegen die migrierende DB würde bis zum 30-s-Sperrtimeout hängen und den Server als hängend erscheinen lassen.
- **`_start_zustand["laeuft"]` startet auf `False`** und wird erst im `lifespan` gesetzt. Der Wert heisst „läuft gerade", nicht „steht aus" — sonst antwortet alles mit der Warteseite, wo der lifespan nicht ausgeführt wird (Tests binden die App direkt ein; das kostete einmal 144 rote Tests).
- Scheduler und Embedding-Nachlauf starten erst **nach** der Vorbereitung, nicht parallel dazu.
- Tests: `tests/test_startvorbereitung.py`.

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

### Suchbegriffe zerlegen & Null-Treffer-Diagnose (seit v3.4.2)

- **Positivliste statt verbotener Zeichen** (`_TRENNZEICHEN`, `web/main.py`): alles ausser Buchstaben und Ziffern trennt Wörter — genau wie der `unicode61`-Tokenizer beim Indexieren. **Falle, die das ausgelöst hat:** die frühere Bereinigung zählte verbotene Zeichen einzeln auf (`"()*:^` und `.`) und übersah den **Bindestrich**. FTS5 liest `u-wert` als Spaltenfilter → jede Suche mit Bindestrich brach mit `no such column: wert` ab, und die rohe SQLite-Meldung stand wörtlich in der Oberfläche. Unterstriche gehören ebenfalls dazu (`06_Felix` steht im Index als `06` + `felix`).
- Dieselbe Zerlegung nutzen **alle Suchwege** (`_make_fts_query`, `_search_filename`, `_search_like`, `_search_folders`, `_excerpt`) — vorher hatte jeder seine eigene Kopie, mit sichtbar unterschiedlichem Verhalten.
- **Kurze Wörter bekommen keinen Präfix-Stern** (`_MIN_PRAEFIX_LAENGE = 3`, `_fts_wort`). `u*` traf jedes Wort, das mit u beginnt — und, uns, unten — und machte eine Suche nach „u wert" (also U-Wert) wertlos. Als ganzes Token gesucht trifft `u` nur ein alleinstehendes U, genau wie in „U-Wert", das der Indexer ohnehin als `u` + `wert` ablegt. „u-wert" und „u wert" ergeben damit **dieselbe** Abfrage.
- **`_wortmuster()` definiert die Wortgrenze selbst** statt `\b` zu benutzen: für reguläre Ausdrücke ist der **Unterstrich ein Wortzeichen**, für den unicode61-Tokenizer ein Trenner. In einem Ordner `250813_Attika` gäbe es vor „Attika" kein `\b`, obwohl im Index `250813` und `attika` getrennt stehen (genau das liess einen bestehenden Test scheitern). Kurze Wörter müssen ein ganzes Wort sein, längere dürfen Wortanfang sein: „wert" trifft „Werte", aber nicht „Bewertung".
- **Ordnersuche und Hervorhebung folgen derselben Regel.** Vorher: `_search_folders` warf Wörter mit einem Zeichen weg und suchte den Rest als Teilstring (`%wert%` → „Bewertung", „Schalldaemmwerte"); `_excerpt` markierte bei „u wert" das erste beliebige „u" im Text, also meist gar nicht die Fundstelle.
- `_suchfehler()` übersetzt Datenbankfehler in einen verständlichen Satz; der Wortlaut geht ins Log.
- **Null-Treffer-Diagnose** (`_leertreffer_diagnose`): Mehrwortsuchen sind UND-verknüpft, ein einziger unbekannter Begriff lässt alles ins Leere laufen. Bei null Treffern wird pro Wort ein Existenztest gefahren (LIMIT 1, Volltext **und** Dateiname, unter den aktiven Filtern) und der schuldige Begriff genannt, plus Trefferzahl ohne ihn. Realer Anlass: Suche nach `260902 Afo Eingabe`, Datei heisst `260209 Afo Eingabe…`.
  **Die engere Suche wird bewusst NICHT automatisch ausgeführt** — eine Trefferliste für eine andere als die gestellte Frage ist in einem Archiv gefährlich (jemand schliesst daraus, das gesuchte Dokument existiere). Angebot per Link, nicht stille Korrektur.
  Läuft nur auf dem Null-Treffer-Pfad; erfolgreiche Suchen kostet es nichts.
- **Zweiter Fall (seit v3.4.3): kommt jedes Wort vor, aber nie gemeinsam**, wird pro Wort einmal ohne dieses Wort gesucht und der am stärksten einschränkende Begriff genannt (`art: "zu_eng"`). Beispiel aus der Praxis: `treppenhaus hochhaus lift plan keller rietbachstrasse` — ohne den Strassennamen gibt es Treffer. Begrenzt auf ≤ 6 Wörter, darüber lohnen die zusätzlichen Abfragen nicht.
- **Der Knopf im Hinweis darf NICHT auf `/search?q=…` verlinken.** `/search` liefert nur das Ergebnis-Fragment für HTMX; ein `href` dorthin zeigte im Browser die rohe, ungestaltete Teilseite. Stattdessen `sucheErsetzen()` (index.html): Suchfeld setzen und `htmx.trigger(feld, 'search')`. Die Anfrage geht über ein `data-`Attribut — `|tojson` im `onclick` zerreisst das Attribut mit seinen Anführungszeichen (der Knopf tat dann gar nichts).
- Tests: `tests/test_suchbegriffe.py`, `tests/test_leertreffer_diagnose.py`.

**Nebenbefund (v3.4.3):** der Handler `htmx:beforeRequest` prüfte `document.getElementById('ai-toggle').checked`. Diese Checkbox wurde vor längerem durch die beiden Modus-Knöpfe ersetzt, der Handler aber nie nachgezogen — seither warf **jede** Suche einen TypeError und die Ladeanimation der KI-Suche erschien nie. Jetzt über `#mode-ki-btn.classList.contains('active')`.

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
- **Migrationen 026–028 (Datenbank-Verkleinerung, v3.4.0).** An der **echten Produktivdatenbank gemessen** (240'308 Dokumente, 652'783 Chunks): **5,97 → 4,46 GB (−25,3 %, 1,51 GB)**. Migration 2,9 min, anschliessendes `VACUUM` 15 s. Dev-DB zum Vergleich: 286 → 192 MB (−32,7 %).
  Aufteilung der Ersparnis, nachgemessen per `dbstat`: **float16 −1002 MB** (652'783 × 1536 statt 3072 Bytes), **documents_fts samt Index ~−495 MB**, `created_at` ~−13 MB.
  **Falle:** eine frühere Schätzung ging von −37 % aus, weil die FTS-Textkopie auf ~850 MB taxiert wurde. Diese Zahl stammte aus einer Abfrage mit Vorrang-Fehler (`sum(a)+sum(b)/1000000`), deren Ergebnis für jeden beliebigen Textumfang praktisch gleich aussieht — sie stützte die Annahme also nie. Real war die Kopie rund 350 MB. Lehre: Grössenanteile mit `dbstat` messen, nicht aus zusammengesetzten `length()`-Summen herleiten.
  - **026 — `documents_fts` ohne `content`.** Die Tabelle ist eigenständig (kein `content=`) und speicherte deshalb eine **vollständige zweite Kopie aller Dokumenttexte** samt Index (produktiv ~350 MB Text plus ~150 MB Index). Gesucht wurde darauf nie: die einzige lesende Stelle ist `_search_filename()` (`web/main.py`) mit `filename:`-Spaltenfilter, die Volltextsuche läuft über `chunks_fts`. Die drei Trigger auf `document_content` entfielen ersatzlos. **Kein UPDATE-Trigger nötig:** `documents.filename` wird nirgends geändert (Identität über Hash, Umbenennung = neuer Pfad).
  - **027 — Embeddings float32 → float16.** Grösster Einzelposten (produktiv ~2 GB von 6,5 GB). **KRITISCH: der Datentyp steht nirgends in der DB.** Ein doppelt konvertierter Blob wird zu Unsinn, und `embedder.py` baut aus allen Zeilen EINE Matrix → eine einzige falsche Zeile legt die semantische Suche lahm. Die Migration hält deshalb die **Quell-Blobgrösse vor der ersten Umwandlung** als eigene `_migrations`-Zeile (`027_quelllaenge_<n>`) fest — ohne diesen Merker wäre ein Abbruch zwischen „alles umgewandelt" und „in `_migrations` eingetragen" nicht von „noch nichts getan" unterscheidbar (`_apply()` legt keine Transaktion um die Migration).
    **Qualität gemessen** (22'582 Chunks, 200 Anfragen): max. Score-Abweichung 5,3e-05; wo sich die Reihenfolge dreht, beträgt der Score-Abstand der getauschten Treffer ≤ 8,6e-06 — es sind also ausschliesslich Gleichstände. Top-10 als Menge in 186/200 Fällen identisch.
  - **028 — `document_chunks.created_at` entfernt.** Wird nirgends gelesen; produktiv ~13 MB, bei 1 Mio. Dokumenten dreistellig. Braucht SQLite ≥ 3.35 (Bundle: 3.53) — schlägt es fehl, bleibt die Spalte stehen statt abzubrechen.
  - **Nach der Migration ist ein `VACUUM` nötig**, damit die Datei tatsächlich schrumpft — gelöschte Seiten werden sonst nur als frei markiert.
- **`queries.upsert_path` (Fix):** hängt Pfad per `ON CONFLICT(path) DO UPDATE` auf das aktuelle Dokument um + räumt verwaiste Alt-Version auf. Vorher (`INSERT OR IGNORE`) blieb der Pfad bei geänderten Dateien auf der alten Version → neues Dokument verwaist.
- **Beim Projekt-Löschen:** `mail_scan_config` hat kein CASCADE → separat löschen. Deletion großer Projekte im Hintergrund-Thread (`_delete_project_bg`) mit Polling.

### Sicherung & Umzug (`db/backup.py`, seit 3.3.0)

Eine Mechanik für zwei Fälle: wöchentliche Sicherung gegen Rechnerausfall, und Umzug des Servers auf einen anderen Mac.

- **`VACUUM INTO` statt Dateikopie.** WAL-Modus: ein blosses Kopieren von `archivio.db` bei laufendem Server verliert die letzten Transaktionen still. Läuft bei laufendem Server, ~229 MB/s gemessen.
- **Lokal vacuumen, dann übertragen.** Direkt aufs Netzlaufwerk zu vacuumen ist vielfach langsamer (SQLite schreibt seitenweise über SMB) und bricht bei kurzem Mount-Verlust ab.
- **Genau eine Sicherung, aber nie ohne gültige Kopie:** neuer Stand entsteht vollständig in `Archivio-Sicherung.neu`, dann zwei Umbenennungen (`→ .alt`, `.neu → final`, `.alt` löschen). `_recover_interrupted()` holt `.alt` zurück, falls dazwischen abgebrochen wurde. **`PRAGMA quick_check` vor dem Einwechseln** — ohne das überschreibt eine schleichend defekte DB irgendwann den letzten guten Stand.
- **Nie Mail-Passwörter in der Sicherung** (`_sanitize_config`). Beim Import werden sie abgefragt; Konten ohne Passwort werden in den Einstellungen und in der Diagnose als „Passwort fehlt" markiert — sonst läuft der Mail-Scan nach einem Umzug wochenlang ins Leere.
- **Reihenfolge der Prüfungen beim Import ist Absicht:** erst die billigen (Manifest, Version, Zielzustand, Pfade), **zuletzt** der `quick_check`. Der liest die ganze Datei und braucht bei 5,6 GB von einer externen Platte real **253 s** — lief er vorher, wartete man diese Zeit, bevor überhaupt nach einem fehlenden Ordner gefragt wurde, und nach der Antwort gleich nochmal. Vor jeder Veränderung läuft er weiterhin.
- **Import läuft asynchron** (`_import_state` + `GET /api/backup/import-status`, Muster wie die Sicherung). Vorher hing er in der Antwort des POST: wer die Seite während des Einspielens wechselte, verlor den Fortschritt — die Arbeit lief im Thread weiter (ein Thread lässt sich nicht abbrechen), aber die Oberfläche zeigte wieder den alten Stand. Die Seite fragt den Zustand auch beim Laden ab und nimmt einen laufenden Import wieder auf.
- **Der Server startet sich nach erfolgreichem Import selbst neu** (`_neustart_nach_import`, `os._exit(0)` nach 4 s). Nötig, weil der laufende Prozess die ALTE Datenbankdatei noch offen hat — ohne Neustart zeigt die Oberfläche weiter den Stand von vorher. Greift nur, wenn `ARCHIVIO_SUPERVISED=1` gesetzt ist (von `server_app.py::_env()`); ein handgestartetes uvicorn bliebe sonst weg. Der Watchdog startet binnen 15 s neu, die Oberfläche wartet darauf und lädt dann neu.
- **Import ersetzt, führt nie zusammen.** Alle Prüfungen (Version, `quick_check`, Zielzustand, Pfade) laufen **vor** jeder Veränderung. Die bisherige DB wird umbenannt (`.vor-import-<zeitstempel>`), nie überschrieben — **inklusive `-wal`/`-shm`**, sonst ist die Sicherheitskopie mit „disk I/O error" unlesbar.
- **Pfadumschreibung (`rewrite_paths`)** — vollständige Liste: `projects.path`, `document_paths.path`, `ignored_paths.path`, `norm_folders.path`, `block_rules.value` **nur** bei `type='folder'` (bei `file` steht ein SHA256, bei `pattern` ein Glob), dazu in der config `scanner.base_folders[].path`, `projects[].path`, `rubrica.db_path`. **Nicht** umschreiben: `scanner.excluded_folders` (Ordnernamen/Globs, portabel). Bleibt eine Stelle stehen, zeigt der Index ins Leere und ein späterer Scan löscht die verwaisten Einträge samt Foto-Tags (`_cleanup_missing_files`).
- **Welche Pfade geprüft werden:** `manifest.base_folders`; ist die Liste leer (kommt vor), ersatzweise die Elternordner aus `projects.path` (`_paths_to_verify`).
- **`database.path` und `server` bleiben lokal** — wo die DB liegt, ist Eigenschaft des Rechners, nicht der Sicherung.
- Zeitplan in `web/main.py::_maybe_run_weekly_backup` (wöchentlich, 1 h nach dem Nachtscan, verschiebt sich bei laufendem Scan). Laufzeitzustand in `backup_state.json` im Datenverzeichnis — **bewusst nicht in der config.yaml**, weil `settings_save()` ganze Schlüssel neu schreibt.

### Deinstallation (`scripts/deinstallieren.sh`, seit 3.3.1)

Bedient wird sie über **„Archivio Deinstallieren.app"** (AppleScript-App, wird mit dem Server installiert) — die Kundenbüros haben keine IT-Abteilung, ein abzutippender Terminal-Befehl ist dort unbrauchbar. Der Knopf in den Einstellungen öffnet sie über den Helper-Endpunkt `/deinstallation-oeffnen`. Das Shell-Skript liegt als **Kopie in dieser App** (nicht nur im Server-Bundle): der Deinstallierer muss weiterlaufen, während er den Server entfernt.

Zwei Betriebsarten, **in beiden wird das Datenverzeichnis entfernt**: `--komplett` (Testrechner/Ausmusterung — auch Helper, MCP-Eintrag und Quick Action weg) und `--arbeitsplatz` (nach einem Umzug — nur der Helper samt Einstellungen und MCP-Anbindung bleibt). `--simulation` zeigt den Ablauf, ohne etwas anzufassen; `--ohne-rueckfrage` überspringt die getippte Bestätigung (nur für die GUI, die zweimal nachgefragt hat).

- **Zwei Rechtelagen, zwei Wege:** alles unter `~` per `mv` in den Papierkorb; die **`.app`-Bundles in `/Applications` gehören root** (vom pkg angelegt) — ein `mv` als Benutzer scheitert dort. Dafür übernimmt der Finder per AppleScript, der bei Bedarf selbst nach dem Passwort fragt. Genau dieser Fall wurde beim Sandbox-Test gefunden, nachdem die erste Fassung die Programme stillschweigend liegengelassen hätte.
- **Nichts wird mit `rm` gelöscht** — alles landet im Papierkorb und bleibt bis zu dessen Leerung zurückholbar.
- Zeigt vor dem Zugriff Grösse und Dokumentzahl der Datenbank sowie den Sicherungszustand aus `backup_state.json` und warnt ausdrücklich, wenn keine bestätigte Sicherung existiert.
- Darf **nicht** mit `sudo` laufen (Anmeldeobjekte und Autostart gehören dem angemeldeten Benutzer) — das Skript bricht dann ab.

---

## 12. Tests

- `tests/` mit pytest. **`conftest.py`:** setzt `ARCHIVIO_DATA_DIR` (wird an spawn-Worker vererbt!) + eigene config.yaml, Datenverzeichnis GETRENNT von den gescannten Dateien. **Falle:** ohne das schreiben spawn-Worker in die echte Repo-`archivio.db` (Monkeypatch überquert Prozessgrenze nicht) → grüne, aber wertlose Tests.
- `test_walker.py`, `test_mail_delete.py`, `test_hasher.py` grün. `test_search_recall.py` braucht Ollama + echte DB (`ORDER BY RANDOM()`) → flaky/skip, ist KEINE Regression.
- Stand v3.4.0: **442 Tests** grün (`pytest tests/ -q --ignore=tests/test_search_recall.py`).
- **Dev-`.venv` läuft seit v3.4.0 auf Python 3.14.5 mit SQLite 3.53.1 — derselben SQLite-Version wie das ausgelieferte Bundle** (vorher 3.9.2/SQLite 3.34). Nötig geworden mit den FTS5-Änderungen und `ALTER TABLE … DROP COLUMN` (braucht ≥ 3.35): vorher hätte lokal grün sein können, was beim Kunden bricht. Neu aufsetzen: `/opt/homebrew/bin/python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
- **`requests` und `zeroconf` fehlten in `requirements.txt`** (standen nur in den `EXTRAS` des Build-Skripts) → eine frische Dev-venv konnte die Menubar-/Discovery-Tests nicht sammeln. Seit v3.4.0 dort eingetragen.

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
