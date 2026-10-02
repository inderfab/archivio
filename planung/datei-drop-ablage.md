# Auftrag: Datei-Drop mit Ablage-Vorschlag (ohne KI) — Umsetzung für Claude Code

> **Für Claude Code.** Dieses Dokument ist der vollständige Arbeitsauftrag. Lies es ganz, bevor
> du Code anfasst. Ablageort im Repo: `planung/datei-drop-ablage.md`. Die Fixture
> `musterordner_strut.txt` kommt nach `tests/fixtures/musterordner_strut.txt`.
>
> Stand: 2026-10-01 · Basis: Branch `feat/datei-drop` (Commit `0ba1ceb`, „Zwischenstand“) auf v3.7.4

---

## 0. Bevor du anfängst

1. Lies `CLAUDE.md` und `PROJEKT_STATUS.md`, vor allem §6 (Scanner), §11 (Datenbank), §12 (Tests),
   §16 (Sicherheit). Halte die dort beschriebenen Fallen ein.
2. Arbeite auf einem neuen Branch `feat/ablage-vorschlag`, abgezweigt von `feat/datei-drop`
   (nicht von `main`, und nicht direkt auf `main`).
3. Lass die Tests einmal laufen, damit du einen grünen Ausgangsstand hast:
   `pytest tests/ -q --ignore=tests/test_search_recall.py`
4. Arbeite die Etappen in **genau dieser Reihenfolge** ab (§9). Jede Etappe endet mit grünen
   Tests und einem eigenen Commit. **Etappe 4 (Messung) kommt vor dem Scoring.** Das ist
   Absicht: Ohne Basiswert lässt sich keine Verbesserung belegen.

### Harte Regeln

- **Keine KI:** kein LLM, keine Embeddings, keine ML-Bibliothek (kein sklearn, kein numpy-Zwang).
  Nur Zählen, Regex und Wörterbücher, in reinem Python und SQLite. Alles deterministisch.
- **Jeder Vorschlag ist begründbar:** Zu jeder Option gibt es 1–3 Gründe in Klartext,
  zum Beispiel „Vorgänger `…_b.pdf` liegt hier“ oder „214 ähnliche Pläne in *Planstände*“.
- **Nie automatisch verschieben.** Die Ablage passiert immer erst nach einem Klick oder Enter.
- **Der Vorschlagspfad fasst das NAS nicht an.** Kein `os.walk` im Vorschlagspfad. Erlaubt ist
  nur ein `exists()` auf höchstens 10 Kandidaten. Die Struktur kommt aus der DB.
- **Der Scan darf nicht spürbar langsamer werden.** Miss das (Etappe 2).
- **Nichts Büro-Spezifisches hart codieren.** Strut-Eigenheiten kommen als Daten rein
  (Vorlage, Wörterbuch, Konfiguration), nicht als `if`-Zweige. Das Feature muss bei anderen
  Büros mit anderer Struktur funktionieren.
- `ARCHIVIO_UPLOAD_DRY_RUN=1` bleibt erhalten und muss auch im neuen Ablauf greifen.
- `config.yaml` und `*.db` werden **nie** committet.

---

## 1. Ziel aus Nutzersicht

Ein Mitarbeiter zieht eine Datei (oder mehrere) auf das **Archivio-Symbol des Helpers** in seiner
Menüleiste. Der Browser öffnet sich mit einer Ablage-Seite:

- **Eindeutig:** ein vorausgewählter Zielordner. Enter legt ab.
- **Teilweise klar:** „Sicher **211 Emmenhof › 51 Ausführung**. Ordner wählen:“ gefolgt von 2–3
  Optionen mit Begründung.
- **Unklar:** „Sicher Projekt **211 Emmenhof**, Ordner unklar.“ Der Ordnerbrowser steht offen im
  Projekt.
- **Projekt unklar:** die 3 wahrscheinlichsten Projekte zur Auswahl. Lieber ehrlich „weiss nicht“
  als falsch und sicher.

Das Ablegen muss schneller gehen als selbst suchen und so oft wie möglich auf Anhieb stimmen.
Die Qualität wird gemessen (§7), nicht geschätzt.

---

## 2. Ausgangslage im Branch `feat/datei-drop`

| Teil | Wo | Was es tut | Entscheid |
|---|---|---|---|
| Drop-Ziel auf dem Menüleistensymbol | `shared/menubar_bridge.py::register_drop_target` | Eigene NSView mit Drag&Drop, rumps-Menü bleibt nutzbar | **Behalten**, zusätzlich im **Helper** registrieren (§3) |
| Drop-Callback | `menubar/server_app.py::_on_file_dropped` | Öffnet `/dashboard/upload?src=<lokaler Pfad>` | Bleibt als Sonderfall „Drop direkt am Server-Mac“ |
| Projekt-Vorschlag | `scanner/walker.py::suggest_project_for_file` | Führende Nummer, sonst FTS-ODER-Abfrage | Wird **Signal** im neuen Projekt-Scoring (§6.2) |
| Ordner-Vorschlag | `scanner/walker.py::suggest_destination_folder` / `_raw` | Wortüberlappung, „zuletzt geändert“, `os.walk` bis Tiefe 3 | **Ersetzen** durch §6. Bleibt als Basiswert in der Messung |
| Sicherheitsnetz | `_nearest_existing_folder` | Läuft nach oben bis zu einem existierenden Ordner | Behalten |
| Extraktion ohne DB | `_extract_with_timeout` | Text ohne DB-Schreiben | Behalten, auf die ersten ca. 3000 Zeichen begrenzen |
| Einzeldatei-Index | `walker.process_file` | Datei sofort durchsuchbar | Behalten |
| Ablage-Seite | `web/dashboard.py` (`upload_page`, `upload_columns`, `upload_submit`), `dashboard_upload.html` | Spaltenbrowser, Namensmuster, Kopie behalten | Umbauen (§8) |
| Namensmuster | Einstellungen → Datei-Ablage | `{projektnummer}_{dateiname}_{datum}` | Behalten |

**Der Kernfehler der aktuellen Logik:** Es gibt kein Modell der Struktur. Verglichen werden nur
einzelne Datei-Fakten. Gleich heissende Ordner oder Sammelordner lassen sich so nicht
unterscheiden, und leere Vorlage-Ordner kennt die Logik gar nicht, weil die DB nur Ordner mit
Dateien sieht.

---

## 3. Architektur-Korrektur: Drop gehört in den Helper

**Problem:** Mitarbeiter-Macs haben nur den Helper (PROJEKT_STATUS §2). Der aktuelle Ablauf
übergibt dem **Server** einen lokalen Pfad (`src=/Users/…/Desktop/x.pdf`). Diese Datei liegt
aber auf dem Mitarbeiter-Mac, der Server kann sie nicht lesen. Der Ablauf funktioniert also nur,
wenn die Datei am Server-Mac selbst gezogen wird.

**Neuer Ablauf** (Server und Clients sehen das NAS unter demselben `/Volumes/…`-Pfad. Davon geht
schon `/open` bzw. `/reveal` im Bridge aus):

```
Helper (Client)                         Server                         Browser
───────────────                         ──────                         ───────
Drop → für jede Datei:
  sha256 (≤ 200 MB, sonst nur Meta)
  POST /api/ablage/analyse  ─────────►  Staging: DATA_DIR/ablage_staging/<token>/
   (multipart: Datei ≤ 50 MB             Extraktion (≤ 3000 Zeichen), Features,
    für Textformate, sonst nur           Vorschläge berechnen, in ablage_vorgang
    Metadaten: name,size,mtime,hash)     speichern (TTL 24 h)
                             ◄─────────  {token}
  merkt sich token → src (In-Memory)
  open {server}/dashboard/ablage?t=<token>[,<token>…]  ─────────────────────►  Seite zeigt Vorschläge
                                                                                Nutzer bestätigt
                                         POST /api/ablage/<token>/bestaetigen ◄─ {dest, dateiname, kopie, vorgaenger_archivieren}
                                          prüft: dest liegt in einem Projekt,
                                          speichert Entscheid
                                                                   ◄─ fetch(HELPER + '/ablage/ausfuehren', {token})
Helper: GET {server}/api/ablage/<token>  (holt dest/dateiname vom SERVER, nicht vom Browser)
  prüft: token gehört zu einer selbst registrierten Datei
  verschiebt bzw. kopiert src → dest (eindeutiger Name, siehe _unique_dest)
  optional: Vorgänger → z_Archiv-Geschwisterordner
  POST /api/ablage/<token>/abgelegt {final_path} ─►  process_file() + ablage_log
                                                                   ─► Seite: „✓ abgelegt“
```

**Sicherheit (wichtig):** Der lokale Bridge-Server antwortet mit
`Access-Control-Allow-Origin: *`. Jede Webseite könnte also `localhost:44380` aufrufen. Ein
Endpunkt, der **beliebige** `src/dest`-Pfade aus dem Request verschiebt, wäre ein Loch. Darum gilt:

- `/ablage/ausfuehren` nimmt **nur einen Token** entgegen.
- `src` kommt aus der Token-Tabelle, die der Helper selbst im Speicher führt.
- `dest` kommt vom Server, und der Server hat geprüft, dass es innerhalb eines Projektpfads liegt.

Schreibe dafür einen Test.

Weitere Punkte:

- Ein Drop direkt am Server-Mac (bestehender `_on_file_dropped`) läuft über denselben
  Server-Code, nur ohne Upload: Der Server liest `src` lokal, der Server verschiebt.
  **Eine** Vorschlags-Engine, zwei Transportwege.
- Der Helper bekommt `register_drop_target` aus `shared/menubar_bridge.py` und ebenfalls die
  Registrierung über `rumps.events.before_start`.
- Helper-Version hochzählen (`helper/VERSION`), siehe PROJEKT_STATUS §3: Das ist eine echte
  Helper-Änderung.
- Der Helper hat **keine** PDF-Bibliotheken und soll auch keine bekommen. Die Extraktion bleibt
  auf dem Server, deshalb der Upload kleiner Textdateien. Grosse oder reine Metadaten-Formate
  (`.pln`, `.dwg`, `.ifc` …) werden **nicht** hochgeladen. Für sie reichen Name, Grösse, mtime
  und Hash.
- **Mehrere Dateien in einem Drop:** ein Browser-Tab mit allen Dateien, nicht ein Tab pro Datei.
  Die Dateien werden gemeinsam behandelt (§6.6).

---

## 4. Datenmodell (Migrationen 030 ff.)

Halte das Muster in `db/migrations.py` ein (`_apply(conn, "030_…", _m030)`). Ergänze ausserdem
`db/schema.sql` für frische Datenbanken. SQLite 3.53 (PROJEKT_STATUS §12).

```sql
-- 032_ablage_ordner: jeder Ordner, den der Scan sieht, AUCH leere und ausgeschlossene
CREATE TABLE ablage_ordner (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path          TEXT    NOT NULL UNIQUE,        -- NFC-normalisiert!
    parent_id     INTEGER REFERENCES ablage_ordner(id) ON DELETE CASCADE,
    rel_path      TEXT    NOT NULL,               -- relativ zur Projektwurzel
    depth         INTEGER NOT NULL,
    name          TEXT    NOT NULL,
    label         TEXT    NOT NULL,               -- normalisiert, ohne Ordnungspräfix (§5.1)
    praefix       TEXT    NOT NULL DEFAULT '',    -- abgetrenntes Präfix ("51a", "b", "1.6")
    codes         TEXT    NOT NULL DEFAULT '',    -- erkannte Codes, z.B. BKP "211" / "221.1" (JSON-Liste)
    slot_id       INTEGER REFERENCES ablage_slot(id),
    art           TEXT    NOT NULL DEFAULT 'normal'
                      CHECK (art IN ('normal','archiv','trenner','ausgeschlossen')),
    datei_anzahl  INTEGER NOT NULL DEFAULT 0,     -- direkte Dateien (aus documents abgeleitet)
    letzte_aenderung TEXT,
    zuletzt_gesehen  TEXT NOT NULL               -- beim Scan gesetzt; veraltete = nicht mehr vorhanden
);
CREATE INDEX idx_ablage_ordner_project ON ablage_ordner(project_id);
CREATE INDEX idx_ablage_ordner_slot    ON ablage_ordner(slot_id);

-- 033_ablage_slot: die büroweite "Vorlage" (aus Musterordner importiert und/oder aus Projekten hergeleitet)
CREATE TABLE ablage_slot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label_pfad  TEXT NOT NULL UNIQUE,   -- z.B. "ausfuehrung/planstaende" (Labels, mit "/" verbunden)
    rolle       TEXT NOT NULL,          -- letztes Label, z.B. "planstaende" (Back-off-Ebene)
    anzeige     TEXT NOT NULL,          -- häufigster Original-Name, z.B. "51a_Planstände"
    abdeckung   REAL NOT NULL DEFAULT 0,-- Anteil Lern-Projekte, in denen der Slot existiert
    aus_vorlage INTEGER NOT NULL DEFAULT 0
);

-- 034_ablage_stats: Merkmalszählungen, gewichtet nach Alter (§6.3)
CREATE TABLE ablage_stats (
    ebene    TEXT    NOT NULL CHECK (ebene IN ('ordner','slot','rolle','global')),
    key_id   INTEGER NOT NULL,          -- ablage_ordner.id / ablage_slot.id / Hash(rolle) / 0
    merkmal  TEXT    NOT NULL,          -- z.B. "ext:.pdf", "tok:grundriss", "plantyp:GR", "bkp:211", "dom:ingenieur-xy.ch"
    gewicht  REAL    NOT NULL,
    PRIMARY KEY (ebene, key_id, merkmal)
) WITHOUT ROWID;

-- 035_ablage_vorgang + ablage_log + ablage_regel
CREATE TABLE ablage_vorgang (           -- ein Drop, bis zur Ablage (TTL 24 h, dann aufräumen)
    token TEXT PRIMARY KEY, erstellt TEXT NOT NULL, host TEXT, dateiname TEXT NOT NULL,
    groesse INTEGER, mtime TEXT, hash TEXT, staging_pfad TEXT,
    merkmale TEXT NOT NULL,             -- JSON
    vorschlag TEXT NOT NULL,            -- JSON (Ausgabe von §6.5)
    entscheid TEXT,                     -- JSON {dest, dateiname, kopie, vorgaenger_archivieren}
    status TEXT NOT NULL DEFAULT 'offen' CHECK (status IN ('offen','bestaetigt','abgelegt','abgebrochen'))
);
CREATE TABLE ablage_log (               -- dauerhaft, für Messung & Lernen
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, host TEXT,
    dateiname TEXT, endung TEXT, merkmale TEXT, vorschlag TEXT,
    gewaehlt_pfad TEXT, gewaehlt_rang INTEGER,   -- 1 = Top-1, 2/3, 0 = nicht in Vorschlägen
    projekt_richtig INTEGER, dauer_ms INTEGER
);
CREATE TABLE ablage_regel (             -- explizite Regeln (vom Admin oder bestätigter Vorschlag)
    id INTEGER PRIMARY KEY AUTOINCREMENT, merkmal TEXT NOT NULL, slot_label_pfad TEXT NOT NULL,
    quelle TEXT NOT NULL CHECK (quelle IN ('admin','gelernt')), aktiv INTEGER NOT NULL DEFAULT 1,
    erstellt TEXT NOT NULL, treffer INTEGER NOT NULL DEFAULT 0,
    UNIQUE(merkmal, slot_label_pfad)
);
```

Beim Löschen eines Projekts verschwinden dessen Ordner über `ON DELETE CASCADE`. Teste das
zusammen mit `test_project_archive.py`.

---

## 5. Strukturmodell

### 5.1 Ordnernamen normalisieren (`scanner/ablage/normalisieren.py`)

`zerlege(name) -> (praefix, label, codes)`. Das ist das Herzstück, deshalb gründlich und mit
vielen Tests (Fixture §10).

1. **Unicode:** Zuerst NFC. **Die Musterordner-Pfade kommen von macOS als NFD** (geprüft).
   Pfade in DB und Dateisystem können also NFD oder NFC sein. Jeder Vergleich läuft über eine
   einzige Funktion `pfad_schluessel()`.
2. Umlaute **vor** dem Entfernen der Diakritika ersetzen: `ä→ae, ö→oe, ü→ue, é→e`.
   **Falle:** In umgekehrter Reihenfolge wird aus „Planstände“ das Label „planstande“, aus
   „Planstaende“ aber „planstaende“. Das sind zwei Slots für denselben Ordner (im Prototyp so
   passiert).
3. Kleinbuchstaben, alle Nicht-Alphanumerischen Zeichen durch ein Leerzeichen ersetzen,
   Leerzeichen zusammenfassen. „HLSK-Ingenieur“ wird zu „hlsk ingenieur“.
4. **Ordnungspräfixe** von vorne abtrennen, auch wiederholt: `51`, `51a`, `33.1`, `1.6`, `a`,
   `01`, `z`, `Z`. Ein einzelner Buchstabe gilt nur als Präfix, wenn danach `_`, `-` oder ein
   Leerzeichen folgt. Ergebnis: `51a_Planstände` → (`51a`, `planstaende`) und
   `51_Bauingenieur` → (`51`, `bauingenieur`).
5. **Codes** separat erkennen und **nicht** zum Label machen. BKP-Muster: `211`,
   `201_209` (Bereich), `221_1` / `221.1` (Unterposition), `213.6 + 215.6`,
   `222-223-224`, `258 + 358`. So wird `201_209_Baugrubenaushub_Rodungen_Demontagen` zu
   Label `baugrubenaushub rodungen demontagen` mit Codes `["201-209"]`.
   **Falle:** `273.4 Türen in Holz` und `273_4_Wohnungstueren` liegen beide in der Vorlage und
   haben denselben Code, aber eine andere Bedeutung. Der Code allein ist deshalb nie der
   Schlüssel, das Label entscheidet.
6. **Art bestimmen:**
   - `archiv`: Label in {`archiv`, `alt`, `old`, `ueberholt`}. Die Liste ist
     konfigurierbar (`ablage.archiv_labels`). Achtung: `00_ungueltig` ist bei Strut ein **Status-Ordner** für
     Kontrollpläne und ein legitimes Ziel. `ungueltig` gehört deshalb **nicht** in die
     Standardliste.
   - `trenner`: Namen wie `0-------`, `a ------`, `00-----`, also im Wesentlichen nur
     Bindestriche.
   - `ausgeschlossen`: Ordner, die `_is_excluded_name` trifft.
7. **Rauschen im Namen:** `Farb_Materialkonzept > evt. löschen da im CAD-Dokument enthalten?`
   existiert so in der Vorlage. Abschneiden bei ` > ` (Notiz im Ordnernamen) ist eine Regel
   fürs Label. Der Originalname bleibt in `name`.

### 5.2 Ordner beim Scan erfassen (`scanner/walker.py`)

- `scan_project` iteriert schon über `os.walk`. Sammle pro `dirpath` die Unterordner,
  **bevor** `dirnames` gefiltert wird. Ausgeschlossene Ordner werden mit
  `art='ausgeschlossen'` erfasst, aber nicht betreten. Versteckte Ordner (`.Spotlight-V100`,
  `.fseventsd`, beide in der Musterordner-ZIP vorhanden) werden ignoriert.
- **Nicht pro Ordner schreiben.** Sammle alles im Speicher und schreibe es **einmal am Ende**
  des Projekt-Scans in einer Transaktion, zusammen mit `_cleanup_missing_files`. Vorher
  `zuletzt_gesehen` setzen, danach Ordner mit älterem `zuletzt_gesehen` dieses Projekts löschen.
  Grund: der Autocommit- und `skip_conn`-Aufbau sowie die „database is locked“-Fallen in
  PROJEKT_STATUS §6.
- `datei_anzahl` und `letzte_aenderung` hinterher per SQL aus `document_paths` und `documents`
  ableiten. Dafür sind keine zusätzlichen `stat()`-Aufrufe nötig.
- **Messen:** Scan-Dauer eines grossen Projekts vorher und nachher. Akzeptiert sind höchstens
  +3 %.
- Für bestehende Installationen gibt es einen einmaligen Backfill-Pfad: Beim nächsten normalen
  Scan füllt sich die Tabelle von selbst. Bis dahin fällt die Engine auf
  `document_paths`-abgeleitete Ordner zurück, also ohne leere Ordner.

### 5.3 Vorlage: importieren **oder** herleiten (`scanner/ablage/vorlage.py`)

**Weg A, Musterordner importieren (präzise, für Strut):**
Einstellungen → Datei-Ablage → „Musterordner“. Der Pfad wird über den Helper gewählt
(`/choose-folder` gibt es schon) oder als NAS-Pfad eingetippt. Der Server liest den Baum
(`os.walk`, einmalig, das ist hier erlaubt) und legt `ablage_slot`-Zeilen mit
`aus_vorlage=1` an.

- Bei Strut liegt der Musterordner vermutlich als **`000 Musterordner Objekt`** neben den
  Projekten. Er darf nicht als Projekt 000 in Vorschlägen landen und nicht als Lern-Projekt
  zählen. Erkennen: Der konfigurierte Musterordner-Pfad wird aus beidem ausgeschlossen.
- In der Vorlage liegt `~$000_Ordnerstruktur_Objekt_neu.xlsx`, eine Sperrdatei. Es gibt also
  eine Excel-Datei, die die Struktur beschreibt. **Fabio fragen, ob sie verfügbar ist.** Falls
  sie zu jedem Ordner beschreibt, was hineingehört, ist das die beste Quelle für das Wörterbuch
  (§6.1) und sollte über einen einfachen Import ergänzend eingelesen werden.

**Weg B, Vorlage aus den Projekten herleiten (für Büros ohne Musterordner, Standard):**

1. Lern-Projekte bestimmen: alle Projekte ausser dem Musterordner, ausser der optionalen
   Untergrenze (`ablage.lernen_ab_projektnummer`, **Strut: 184**, denn die Projekte 1–183 haben
   eine alte Struktur) und ausser Projekten mit weniger als 20 Ordnern.
2. Pro Projekt die Menge der `label_pfad`e bilden.
3. Slot = `label_pfad` mit Abdeckung ≥ 0.5 (konfigurierbar). `anzeige` = häufigster
   Originalname.
4. **Automatisches Aussortieren abweichender Projekte** als generischer Ersatz für die harte
   Nummerngrenze: Projekte, deren Jaccard-Ähnlichkeit zur hergeleiteten Vorlage unter 0.3 liegt,
   zählen beim Lernen nicht mit. Danach die Vorlage einmal neu berechnen (2 Iterationen reichen).
   Gibt es Weg A **und** B, gewinnt A für die Struktur. B liefert die Statistik.

**Slot-Zuordnung:** Jeder `ablage_ordner` bekommt die `slot_id` seines `label_pfad` (falls
vorhanden). Projektspezifische Ordner haben keinen Slot und werden über Ordner- und
Rollen-Statistik bewertet.

**Sichtbar machen:** Einstellungen → Datei-Ablage → „Erkannte Struktur“ zeigt den Slot-Baum
mit Abdeckung in % und den Dateien pro Slot. So sieht der Admin, was Archivio „verstanden“ hat.
Das ist zugleich das wichtigste Debug-Werkzeug.

### 5.4 Was der Strut-Musterordner zeigt (als Daten verwenden, nicht hart codieren)

794 Ordner nach Bereinigung (`tests/fixtures/musterordner_strut.txt`). Die Muster:

- **Phasenordner nach SIA 112:** `22_Wettbewerb_Studienauftrag`, `31_Vorprojekt`,
  `32_Bauprojekt`, `33_Baubewilligungsverfahren`, `41_Ausschreibungen`, `51_Ausfuehrung`,
  `53_Inbetriebnahme_Revision`. SIA-Phasennummern sind in der Schweiz verbreitet, deshalb kommt
  eine Phasen-Wortliste ins Standard-Wörterbuch (§6.1).
- **Wiederkehrende Rollen pro Phase mit wechselndem Buchstaben:** `Planstände` ist in 31/32/33/41
  der Buchstabe `a`, in 51 ebenfalls `51a`, aber `Fachplaner` ist `31d`, `33h`, `41e`, `51f`.
  **Darum Label statt Präfix als Schlüssel.** Hinweis: Ausführungspläne-Phasenstände liegen in
  `51_Ausfuehrung/51a_Planstände`. `51b` ist `Berechnungen_Listen`.
- **Querschnitt-Dimension BKP:** dieselben BKP-Ordner in `41d_Devisgrundlagen`,
  `51d_Unternehmer`, `b_Vertraege/2_Unternehmer` und `e_Grundlagen/5_Produkte`. Darin gibt es
  unter `51d_Unternehmer/<BKP>/` die Ordner `01_Kontrollplaene/{00_in_Bearbeitung,
  00_abgeschlossen, 00_ungueltig}` und `02_Ausfuehrung`.
- **Fachplaner-Dimension:** Bauingenieur, Bauleitung, Bauphysiker, Brandschutz,
  Elektroingenieur, HLSK-Ingenieur, Landschaftsplaner, Geologe – Altlasten, Vermessung,
  `z_weitere/…`. Es gibt sie pro Phase (`5xf_Fachplaner/51_X`) und in den Verträgen
  (`b_Vertraege/1_Fachplaner/1.x_X/01_Vertrag`).
- **Phasenunabhängige Bereiche:** `a_Projektorganisation`, `b_Vertraege`, `c_Protokolle`
  (BHS, FPS, BSS, Behörden, intern), `d_Fotografie`, `e_Grundlagen`,
  `f_Publikation_Besichtigungen`, `g_Verkauf_Vermietung`, `h_Projektraum`.
- **Fast jeder Blattordner hat `z_Archiv` oder `Z_Archiv`.** Das ist **nie** das Primärziel,
  aber das Ziel für „Vorgänger archivieren“ (§6.4).
- **Bekannte Inkonsistenzen,** die die Normalisierung abfangen muss (Tests!):
  - `00_in Bearbeitung` / `00_in_Bearbeitung`
  - `Bodenbeläge` / `Bodenbelaege`
  - `Z_Archiv` / `z_Archiv`
  - `230 Schrankensystem ESH` / `230 Schrankensystem`
  - `222-223-224 Spengler…` neben `222_Spenglerarbeiten`
  - `258 + 358 Kücheneinrichtungen` neben `258_Kuecheneinrichtungen`
- **Ablage-Regel des Büros** (aus `0 CAD Daten und Export/z_Info.rtf`, wörtlich sinngemäss):
  > CAD-Datei und Exportordner = immer aktuellster Stand, wird stetig überschrieben, ist
  > phasenunabhängig. Planstände in Phasenordnern = Phasenabschlüsse, wichtige Planfreezes
  > (z.B. Planreview). Versandpläne werden innerhalb der jeweiligen Phase beim
  > Fachplaner/Unternehmer abgelegt.

  Konsequenz: **Für einen Plan-PDF gibt es systematisch drei korrekte Orte, je nach Absicht.**
  Die Absicht lässt sich aus der Datei oft nicht ableiten. Das ist kein Fehler des Algorithmus,
  sondern muss als **benannte Optionen** erscheinen (§6.5, „Absicht-Optionen“). Fabio sagt
  ausserdem, dass die CAD- und PDF-Ablage zwischen Projekten leicht abweicht. Deshalb kommt
  dieser Ort aus der Statistik des konkreten Projekts, nicht aus der Vorlage.

---

## 6. Vorschlags-Engine (`scanner/ablage/`)

Module:

- `normalisieren.py`
- `merkmale.py`
- `vorlage.py`
- `index.py`: Statistik neu aufbauen
- `projekt.py`
- `ordner.py`
- `vorschlag.py`: Orchestrierung und Ausgabeformat

Die alten Funktionen in `walker.py` bleiben, bis Etappe 5 die Messung gewinnt. Danach werden sie
gelöscht und in `scripts/ablage_messung.py` als Basiswert eingefroren.

### 6.1 Merkmale (`merkmale.py`)

`merkmale(dateiname, groesse, mtime, text: str|None, mail: dict|None) -> dict[str, float]`.
Die Wörterbücher stehen in `config/ablage_woerterbuch.yaml`. Es gibt ein Standard-Wörterbuch
(Schweiz/SIA) und ein optionales Büro-Wörterbuch in `config.yaml` unter `ablage.woerterbuch`,
das ergänzt.

| Merkmal | Beispiel (Erkennung) | Präfix |
|---|---|---|
| Endung | `.pdf`, `.pln`, `.eml` | `ext:` |
| Dateinamen-Tokens | normalisiert wie §5.1, Stoppwörter raus, ≥ 2 Zeichen, Zahlen raus | `tok:` |
| Projektnummer | führend `^\d{3}[\s_-]`, Regex konfigurierbar (`ablage.projektnummer_regex`) | `proj:` (nur für §6.2) |
| Phase | VP, BP, BG/BE/Baueingabe, AP/AU/Ausführung, WB/Wettbewerb, Revision; SIA-Zahl im Kontext | `phase:` |
| Plantyp | GR, SN, AN, DET, SIT, UMG, Grundriss, Schnitt, Ansicht, Detail, Situation | `plantyp:` |
| Geschoss | UG, EG, OG1/1.OG, DG, Attika | `geschoss:` (nur „ist Plan“) |
| Massstab | `1:50`, `1-50`, `M50`, `_50_` (nur mit Plan-Kontext) | `massstab` (Flag) |
| Index/Revision | `_a`, `_B`, `Rev3`, `Index C`, `v2` am Ende | `index` (Flag) + für §6.4 |
| Datum | `yymmdd`, `yyyymmdd`, `dd.mm.yyyy`, `yyyy-mm-dd` | `datum` (Flag) |
| Dokumenttyp | Offerte, Rechnung, Protokoll/BHS/FPS/BSS, Vertrag/Werkvertrag, KV/Kostenvoranschlag, Devis/LV, Terminprogramm, Adressliste, Organigramm, Baubeschrieb, Baugesuch, Baubewilligung, Brandschutz, Schutzraum, Gutachten, Grundbuch, Visualisierung, Jurybericht, Kontrollplan … | `doktyp:` |
| BKP | `BKP 211`, `211.1` **mit** BKP-Kontext, plus BKP-**Bezeichnungen** (Baumeister, Gipser, Spengler …). Die Bezeichnungen werden aus den BKP-Ordnernamen der Vorlage **selbst gelernt** | `bkp:` |
| Fachplaner | Bauingenieur/Statik, HLKS/HLSK/Haustechnik, Elektro, Bauphysik/Akustik, Brandschutz, Landschaft, Geologe, Geometer/Vermessung, Bauleitung | `fp:` |
| Mail | Absender-Domain, Betreff-Tokens | `dom:`, `tok:` |
| Inhalt | nur die ersten ca. 3000 Zeichen: Tokens (niedriges Gewicht), Projektnummer und -name im Plankopf | `inh:` |

**Fallen:**

- **BKP gegen Projektnummer:** `BKP_211_Baumeister_Offerte.pdf` gehört nicht zu Projekt 211.
  Eine dreistellige Zahl ist nur dann eine Projektnummer, wenn sie **führend** steht, nicht auf
  „BKP“ folgt **und** das Projekt existiert. Bei Konflikt wird sie zu `bkp:`.
- **Fachplaner-Auftragsnummern** am Dateianfang sehen aus wie Projektnummern. Gibt es zur
  führenden Nummer kein Projekt, ist das kein Signal. Es zählt nicht als „Projekt unbekannt“,
  sondern wird ignoriert.
- **Echte Dateinamen ansehen, bevor die Regex-Liste festgeschrieben wird:** Ziehe aus der
  Dev-DB 300 zufällige Dateinamen aus Projekten ≥ 184 (gruppiert nach Top-Ordner) und prüfe die
  Erkennung daran. Die Beispiele in diesem Dokument sind **erfunden**. Strut-Plannamen-
  Konventionen bei Fabio erfragen, falls die Stichprobe kein klares Muster zeigt.

### 6.2 Projekt bestimmen (`projekt.py`)

Kandidaten bekommen Punkte. Ausgegeben wird eine normierte Verteilung (Softmax) über die
Top-5.

| Signal | Gewicht (Start) |
|---|---|
| Hash existiert schon in Projekt P (Duplikat) | entscheidend, siehe §6.4 |
| Vorgänger (§6.4) in Projekt P | sehr hoch |
| Führende Projektnummer passt zu existierendem Projekt | sehr hoch |
| Projektnummer **und** Projektname im Text-Anfang (Plankopf) | hoch |
| Projektname-Tokens im Dateinamen (zum Beispiel „Emmenhof“) | mittel |
| Mail-Absender-Domain war bisher überwiegend in P | mittel |
| Bisheriger FTS-Abgleich (`suggest_project_for_file`), nur Top-3 mit Anteil | niedrig |
| Projekt aktiv **und** in den letzten 30 Tagen Dateien geändert | Prior, leicht |
| Dieser Rechner (`host`) hat zuletzt in P abgelegt (`ablage_log`, 14 Tage) | Prior, leicht |

Projektnamen-Erkennung im Text: ein **Wörterbuch aller Projektnummern und -namen**
(`projects.name`), gesucht in den ersten 3000 Zeichen. Das ist billiger und genauer als die
FTS-ODER-Abfrage, die nur noch als Rückfall dient.

### 6.3 Statistik aufbauen (`index.py::neu_aufbauen()`)

- Läuft **nach Scan-All** (in `_run_fts_optimize` bzw. danach, unter demselben Scan-Lock, siehe
  PROJEKT_STATUS §6) und auf Knopfdruck in den Einstellungen. Nach jeder Ablage wird inkrementell
  addiert (die abgelegte Datei zählt sofort).
- Für jede Datei mit primärem Pfad in einem Lern-Projekt: Merkmale aus Dateiname, Endung und
  Mail-Metadaten berechnen. **Kein** Inhalt beim Neuaufbau. Das hält ihn schnell, Inhalt wird
  nur beim Drop verwendet.
- Altersgewicht `w = 0.5 ** (alter_tage / 730)`.
- Auf vier Ebenen aufsummieren:
  - `ordner` (der konkrete Ordner)
  - `slot`
  - `rolle` (Label des Blattordners, phasenübergreifend: alle „Planstände“)
  - `global`
- Merkmale mit Gesamtgewicht < 2 verwerfen. Dateien in `archiv`-Ordnern zählen für ihren
  **Elternordner** mit halbem Gewicht. Was im Archiv liegt, lag einmal dort.
- Akzeptanz: Neuaufbau auf der Produktiv-DB in < 60 s auf dem iMac.

### 6.4 Vorstufen vor dem Scoring

1. **Duplikat:** Der Hash existiert bereits. Meldung: „Diese Datei liegt bereits unter X.“
   Optionen: abbrechen (Standard), trotzdem ablegen.
2. **Explizite Regeln** (`ablage_regel`): Wenn ein Merkmal passt und der Slot im Zielprojekt
   existiert, wird der Slot Top-1 mit dem Grund „Regel: …“.
3. **Vorgänger:** gleicher **Stamm** im Zielprojekt. Stamm = normalisierter Dateiname ohne
   Index/Revision, ohne Datum, ohne Kopie-Suffixe `(2)` / `Kopie` / `copy`. Liegt der Vorgänger
   in Ordner X, wird X Top-1 mit sehr hoher Konfidenz (Grund: „Vorgänger `…_b.pdf` liegt hier“).
   Hat X einen Kindordner der Art `archiv`, wird angeboten: „Vorgänger nach z_Archiv
   verschieben“. Das Häkchen ist **nicht** vorausgewählt, ausser die Ablage-Statistik zeigt, dass
   dieses Büro das in diesem Slot regelmässig tut (≥ 70 % der Versionsfolgen). Liegen mehrere
   Vorgänger in verschiedenen Ordnern (Versand an Fachplaner **und** Planstand), werden das
   mehrere Optionen. Das passt zu §5.4.

### 6.5 Scoring und Konfidenz über die Hierarchie (`ordner.py`, `vorschlag.py`)

**Kandidaten:** alle `ablage_ordner` des Zielprojekts mit `art='normal'`, inklusive leerer
Vorlage-Ordner. Ergänzt um **virtuelle Slots** aus der Vorlage, die im Projekt fehlen. Diese
dürfen nur vorgeschlagen werden mit dem Hinweis „Ordner wird angelegt“, und nur, wenn der
Elternordner existiert.

**Score pro Ordner o** (Log-Raum, Naive Bayes mit Back-off):

```
score(o) = log Prior(o) + Σ_m  g(typ(m)) · idf(m) · log P(m | o)

P(m | o) = ( n_ordner(m,o) + α·P(m|slot(o)) + β·P(m|rolle(o)) + γ·P(m|global) )
           / ( N_ordner(o) + α + β + γ )
           (P(m|slot) analog mit Back-off auf rolle/global; Startwerte α=5, β=3, γ=1)

Prior(o) = (Dateien in o bzw. Slot büroweit, geglättet)
         × Phasen-Aktivität(o)        -- Anteil kürzlich geänderter Dateien (Halbwertszeit 60 Tage)
                                         im Phasen-Teilbaum des Projekts; leicht, hilft „aktuelle Phase“
         × Vorlagen-Bonus(o)          -- leerer Vorlage-Ordner in neuem Projekt nicht benachteiligen
```

- Startgewichte `g`:
  - `ext` 1.0
  - `tok` 1.0
  - `plantyp` 2.0
  - `doktyp` 2.0
  - `bkp` 2.0
  - `fp` 2.0
  - `dom` 3.0
  - `phase` 1.5
  - `inh` 0.3
  - Flags 0.5
- `idf` über die Slots: Merkmale, die in allen Slots vorkommen (`ext:.pdf`, Projektname),
  tragen nichts bei.
- **Tuning nur gegen die Messung (§7)** mit der Zeit-Trennung: Gewichte auf dem Trainingsteil
  wählen, auf dem Testteil berichten. Höchstens eine einfache Rastersuche, keine Optimierer.
- Wahrscheinlichkeiten per Softmax über alle Kandidaten.

**Hierarchie-Konfidenz:** Die Wahrscheinlichkeiten werden im Ordnerbaum nach oben summiert.

- `sicher_bis` = der **tiefste** Knoten mit Summe ≥ `ablage.schwelle_sicher` (Start 0.80).
- **Optionen** = die 2–3 wahrscheinlichsten Ordner unterhalb von `sicher_bis`.
- Ist der beste Einzelordner selbst ≥ 0.80, ist es **eindeutig**.

**Absicht-Optionen** (aus §5.4, generisch formuliert): Hat ein Merkmalsprofil in diesem Projekt
mehrere **etablierte** Orte (≥ 15 % der Wahrscheinlichkeit je Ort, verschiedene Rollen), dann
werden sie als benannte Optionen gezeigt statt als „unsicher“. Die Benennung kommt aus der Rolle:

- `0 CAD Daten und Export/01_PDF` → „Aktueller Stand (CAD-Export)“
- `…/Planstände` → „Phasenstand / Planfreeze“
- `…/Fachplaner/…` bzw. `…/Unternehmer/…` → „Versand an …“

Die Bezeichnungen stehen im Wörterbuch pro Rolle, Strut bekommt sie aus der z_Info-Regel.

**Ausgabeformat** (JSON, gespeichert in `ablage_vorgang.vorschlag`):

```json
{
  "fall": "eindeutig | teilweise | ordner_unklar | projekt_unklar | duplikat",
  "projekte": [{"id": 12, "name": "211 Emmenhof", "p": 0.97, "gruende": ["Projektnummer 211 im Dateinamen"]}],
  "sicher_bis": {"pfad": "/Volumes/…/211 Emmenhof/51_Ausfuehrung", "p": 0.91},
  "optionen": [
    {"pfad": "…/51_Ausfuehrung/51a_Planstände", "p": 0.52, "label": "Phasenstand / Planfreeze",
     "gruende": ["Vorgänger 211_GR_EG_b.pdf liegt hier"], "neu_anlegen": false,
     "vorgaenger": "…/211_GR_EG_b.pdf", "archiv_ordner": "…/51a_Planstände/z_Archiv"}
  ],
  "dateiname_vorschlag": "…",
  "dauer_ms": 84
}
```

### 6.6 Mehrere Dateien in einem Drop

Alle Dateien eines Drops werden zuerst einzeln bewertet. Dann gilt: Ist **eine** Datei beim
Projekt eindeutig (≥ 0.95) und die anderen sind unklar, werden die anderen auf dieses Projekt
gezogen. Gleiche Endung **und** gleicher Stamm-Präfix ergeben einen gemeinsamen Ordnervorschlag.
In der UI gibt es pro Datei eine Zeile, und „Alle wie erste“ ist ein Klick.

### 6.7 Altprojekte (≤ 183 bei Strut) und Büros ohne Struktur

Ist das Zielprojekt kein Lern-Projekt oder hat es keinen Slot-Bezug, wird nur mit
`ordner`-Statistik dieses Projekts plus `global` gerechnet. Die Hierarchie-Konfidenz greift
genauso. Ergebnis ist dann meist „Projekt sicher, Ordner unklar“, und das ist korrekt.

---

## 7. Messung (`scripts/ablage_messung.py`), Pflicht vor dem Tuning

Liest die DB **nur lesend** (`file:…?mode=ro`, Pfad per `ARCHIVIO_DB=<pfad>` wie bei
`test_search_recall.py`) und schreibt einen Markdown-Bericht nach `stdout` sowie
`ablage_messung_<datum>.md` (gitignored).

- **Zeit-Trennung:**
  - Statistik nur aus Dateien mit `modified_at < T` (T = vor 90 Tagen).
  - Getestet werden die Dateien danach: Stichprobe 2000, gruppiert nach Top-Level-Slot.
  - Die Testdatei selbst und alles mit `modified_at ≥ T` sind beim Bewerten unsichtbar.
  - Die Ordnerstruktur darf vollständig bekannt sein, denn sie existierte ja.
- Zwei Läufe: **mit** und **ohne** Vorgänger-Signal. Der zweite zeigt, was die Statistik allein
  kann.
- **Kennzahlen,** jeweils gesamt und pro Top-Level-Bereich:
  - Projekt Top-1 / Top-3
  - Ordner Top-1 / Top-3
  - Anteil „eindeutig“ und davon Anteil falsch (**sicher-falsch**, die wichtigste Zahl)
  - Anteil `sicher_bis` korrekt (das heisst, der wahre Ordner liegt darunter)
  - Median- und p95-Laufzeit
- **Basiswert:** dieselben Kennzahlen für die alte Logik aus `feat/datei-drop`
  (`suggest_project_for_file` + `suggest_destination_folder`, ohne `os.walk`-Teil, wenn er die
  Messung verfälscht, und das im Bericht vermerken).
- Zusätzlich nach Livegang: `ablage_log` auswerten (Anteil Top-1 akzeptiert, Rang-Verteilung).
  Als Abschnitt in den Einstellungen anzeigen oder als `GET /api/ablage/statistik`.

**Akzeptanzkriterien** (auf der Strut-DB, Projekte ≥ 184, Lauf ohne Vorgänger-Signal):

| Kennzahl | Ziel |
|---|---|
| Projekt Top-1 | ≥ 95 % |
| Ordner Top-3 | ≥ 85 % |
| Ordner Top-1 | ≥ 65 % |
| sicher-falsch (als „eindeutig“ markiert, aber falsch) | ≤ 3 % der eindeutigen |
| `sicher_bis` korrekt | ≥ 95 % |
| Laufzeit Vorschlag ohne Extraktion (p95) | < 300 ms |
| Extraktion beim Drop | hart begrenzt auf 3 s, danach ohne Inhalt weiter |

Wird ein Ziel verfehlt: Bericht an Fabio mit den 20 häufigsten Fehlertypen (wahrer Slot →
vorgeschlagener Slot). Die Schwellen werden **nicht** stillschweigend gesenkt.

---

## 8. Oberfläche

`/dashboard/ablage?t=<token>[,<token>…]` ersetzt `/dashboard/upload` (alte Route leitet um). Der
bestehende Spaltenbrowser (`_list_browse_level`, `_prerender_browse_columns`) wird
weiterverwendet.

- **Oben:** Dateiname mit Vorschlag nach Namensmuster (bestehend) und Projekt mit
  Wahrscheinlichkeits-Badge (sicher / wahrscheinlich / unsicher).
  - Bei `projekt_unklar`: 3 Projekt-Knöpfe.
- **Mitte:** Pfad-Brotkrumen bis `sicher_bis` (fett), darunter **1–3 Options-Karten** mit Label,
  Pfad ab `sicher_bis`, Gründen und Badge. Karte 1 ist vorausgewählt. Tastatur: `1`/`2`/`3`
  wählen, **Enter legt ab**, `Esc` bricht ab.
- **Unten (eingeklappt, bei `ordner_unklar` offen):** Spaltenbrowser, aufgeklappt **bis
  `sicher_bis`**, nicht bis zur Serverwurzel.
- **Häkchen:** „Kopie am ursprünglichen Ort behalten“ (bestehend), „Vorgänger nach z_Archiv
  verschieben“ (nur wenn zutreffend), „Ordner wird angelegt“-Hinweis bei virtuellen Slots.
- **Nach Ablage:** „✓ abgelegt in …“ mit Link „Im Finder zeigen“ (Helper `/reveal`) und Link
  „Rückgängig“ (innerhalb von 5 Minuten: Helper verschiebt zurück; nur wenn kein Kopie-Modus).
- **Leistung:** Die Seite steht sofort, die Vorschläge sind schon im Vorgang berechnet. Der
  Spaltenbrowser lädt nach (wie bisher).
- Wähle ich einen Ordner manuell im Browser, wird er gespeichert und `gewaehlt_rang = 0`.

---

## 9. Etappen (je ein Commit, Tests grün)

1. **Normalisierung** (`normalisieren.py`) und Tests mit der Fixture. Alle Inkonsistenzen aus
   §5.4 sind abgedeckt, NFD und NFC.
2. **Ordner-Erfassung im Scan** und Migrationen 030/031. Scan-Zeit gemessen (≤ +3 %).
   Cleanup verwaister Ordner.
3. **Vorlage:** Import (Weg A) und Herleitung (Weg B), Slot-Zuordnung, Einstellungsseite
   „Erkannte Struktur“, Lern-Projekt-Filter (`lernen_ab_projektnummer`).
4. **Merkmale** und Wörterbuch-YAML, **Statistik-Neuaufbau** (032), **Messskript mit Basiswert.**
   → Bericht an Fabio: Stichproben-Dateinamen und Basiswerte, bevor es weitergeht.
5. **Scoring:** Vorstufen (Duplikat, Regel, Vorgänger), Hierarchie-Konfidenz, Absicht-Optionen.
   Iterieren gegen die Messung, bis die Akzeptanz erreicht ist. Alte Funktionen in `walker.py`
   danach entfernen (der Basiswert bleibt im Messskript).
6. **Transport:** Helper-Drop, `/api/ablage/analyse`, Staging, Bridge-Endpunkt
   `/ablage/ausfuehren` (nur Token), Vorgang und Status, Aufräumen nach 24 h. Sicherheitstest.
   Drop am Server-Mac über denselben Weg.
7. **UI** (§8), Mehrfach-Drop (§6.6), Rückgängig.
8. **Lernen:** `ablage_log`, inkrementelle Statistik nach Ablage, Regel-Vorschläge (gleiche
   Korrektur 3× mit demselben unterscheidenden Merkmal → „Regel übernehmen?“ in den
   Einstellungen; nie automatisch aktiv).
9. **Abschluss:**
   - PROJEKT_STATUS.md bekommt ein neues Kapitel „Datei-Ablage“ (Architektur, Fallen, Messwerte).
   - `config.yaml.example` um `ablage:` ergänzen (`musterordner`, `lernen_ab_projektnummer`,
     `schwelle_sicher`, `archiv_labels`, `woerterbuch`, `projektnummer_regex`).
   - Server- **und** Helper-Version hochzählen.
   - **Nicht** mergen, Fabio testet zuerst (PROJEKT_STATUS §4).

---

## 10. Tests (Auswahl, alle mit `tmp_db` aus `conftest.py`)

- `test_ablage_normalisieren.py`
  - Tabelle Name → (Präfix, Label, Codes) für mindestens 40 Fälle aus der Fixture
  - NFD = NFC
  - „Planstände“ = „Planstaende“
  - Trenner und Archiv erkannt
  - `00_ungueltig` ist **kein** Archiv
- `test_ablage_vorlage.py`
  - Fixture als Ordnerbaum in `tmp_path` anlegen → Import ergibt 794 Ordner und die erwarteten
    Slots
  - Drei Projekte aus der Vorlage mit Abweichungen (fehlende, umbenannte und zusätzliche
    Ordner) → Herleitung findet die Vorlage
  - Ein Altprojekt mit fremder Struktur wird aussortiert
- `test_ablage_scan_ordner.py`
  - Leere Ordner und ausgeschlossene Ordner (`art`) erfasst
  - Umbenennen auf dem Dateisystem → alter Eintrag weg nach Re-Scan
  - Projekt löschen → Kaskade
- `test_ablage_merkmale.py`
  - BKP gegen Projektnummer
  - Fachplaner-Nummer ohne Projekt
  - Index/Revision
  - Datum
  - Plantyp
  - Mail-Domain
- `test_ablage_vorschlag.py`
  - Synthetisches Büro (3 Projekte aus der Fixture, je ca. 200 Dateien nach Regeln verteilt)
  - Vorgänger schlägt Statistik
  - Duplikat erkannt
  - Hierarchie-Konfidenz liefert „teilweise“
  - Absicht-Optionen für Plan-PDF
  - Leerer Vorlage-Ordner in neuem Projekt wird gefunden
  - z_Archiv nie Top-1
- `test_ablage_transport.py`
  - Bridge `/ablage/ausfuehren` mit fremdem Token → 403
  - Mit `dest` ausserhalb eines Projekts → abgelehnt
  - DRY-RUN verschiebt nichts
  - Namenskollision → `(2)`
- Bestehende Tests (`test_walker*.py`, `test_copy_to_folder.py`) bleiben grün.

---

## 11. Offene Fragen an Fabio (vor bzw. während Etappe 4 stellen, nicht raten)

1. Ist `000_Ordnerstruktur_Objekt_neu.xlsx` verfügbar? Falls sie zu jedem Ordner beschreibt,
   was hineingehört, wird sie ins Wörterbuch übernommen.
2. Gibt es eine Plan-Namenskonvention (Aufbau der Plannummer, Index-Schreibweise)?
3. Wo genau liegt der Musterordner auf dem NAS (Pfad), und ist er als Projekt registriert?
4. Soll „Vorgänger nach z_Archiv verschieben“ standardmässig angehakt sein?
5. Sollen Mails (`.eml`) über den Drop abgelegt werden, oder bleiben sie im IMAP-Weg?
