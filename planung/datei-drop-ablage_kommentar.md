# Datei-Drop mit Ablage-Vorschlag — Kommentar zum Auftrag (Stand nach Etappe 7)

Begleitdokument zu `planung/datei-drop-ablage.md`. Es sagt, was wie beauftragt umgesetzt ist, was anders gelöst
wurde und warum, was im Auftrag unklar oder falsch angenommen war, wo es Probleme gibt und was gut läuft.
Branch `feat/ablage-vorschlag` (von `feat/datei-drop`), nichts gepusht, 785 Tests grün.

---

## 1. Kurzfazit

- **Gebaut ist der ganze Weg**: Datei aufs Menüleisten-Symbol → Analyse → Vorschlag → Ablage-Seite → Ablegen →
  Rückgängig. Er läuft über den Helper (Mitarbeiter-Mac) und über den Server-Mac, mit derselben Engine. Etappen 1–7
  sind fertig und committet, Etappe 8 (Lernen) und 9 (Abschluss) fehlen.
- **Die Qualität erreicht die Ziele des Auftrags nicht**, und zwar nicht wegen eines Fehlers, sondern wegen der
  Daten. Strut legt Dateien tief und datiert ab (`…/51e_Unternehmer/<Firma>/260721_Thema/…`), und die Hälfte der Ziele
  gab es zum Zeitpunkt der Ablage noch nicht. Realistisch bietet der Vorschlag den **richtigen Zweig** an
  (36–44 % unter 3 Optionen), nicht das richtige Blatt (≈ 9 %). Die Ziele wurden mit Fabio entsprechend umgestellt.
- **`sicher_bis` ist verlässlich** (96 % korrekt). Das System sagt also zuverlässig, wie weit es sicher ist, aber es
  ist meist nur bis zum Projekt sicher.
- Das Wichtigste, was die Arbeit gebracht hat, ist **gemessenes Wissen über die echten Strut-Daten**. Mehrere
  Annahmen des Auftrags sind damit widerlegt (Abschnitt 4).

---

## 2. Was gleich geblieben ist (wie beauftragt umgesetzt)

| Punkt | Stand |
|---|---|
| Keine KI (kein LLM, keine Embeddings, keine ML-Bibliothek) | eingehalten: Zählen, Regex, Wörterbücher, SQLite; alles deterministisch |
| Jeder Vorschlag mit 1–3 Gründen in Klartext | ja |
| Nie automatisch verschieben | ja, erst nach Klick oder Enter |
| Vorschlagspfad fasst das NAS nicht an | ja, ein Test schaltet `os.walk` scharf ab; `exists()`/`is_file()` nur auf dem gewählten Ziel |
| Scan nicht spürbar langsamer | gemessen **+2,7 %** (Re-Scan, Worst-Case, Ziel ≤ 3 %), nach einer Optimierung (anfangs +32 %) |
| `ARCHIVIO_UPLOAD_DRY_RUN=1` bleibt | ja, im Helper und am Server-Mac, mit Tests |
| Nichts Büro-Spezifisches hart codiert | Wörterbuch (YAML), Config `ablage.*`, Musterordner als Daten |
| `config.yaml` und `*.db` nie committet | ja; die Strut-DB-Kopie liegt ausserhalb des Repos |
| Etappen-Reihenfolge, Messung vor Scoring | ja, mit eigenem Commit je Etappe |
| Sicherheit der Bridge | nur Token; `src` aus der Helper-Registry, `dest` vom Server, beides nochmals geprüft; Sicherheitstests (fremder Token → 403, Ziel ausserhalb Projekt, gefälschter Server) |
| Datenmodell, Strukturmodell, Vorlage (Weg A/B), Statistik auf 4 Ebenen, Hierarchie-Konfidenz, Ausgabeformat | wie beschrieben |
| Architektur: Drop gehört in den Helper | ja, mit Staging 24 h, Aufräumen, Extraktion hart auf 3 s und 3000 Zeichen |

---

## 3. Was anders gelöst wurde — und warum

1. **Messmethode (§7).** Der Auftrag lässt die Struktur „vollständig bekannt". Gemessen: 93 % der Testdateien lagen
   dann in Ordnern, die zum Stichtag noch nicht existierten (neue datierte Sitzungsordner). Das kommt beim echten Drop
   nie vor. Jetzt gilt die **Struktur zum jeweiligen Stand**, die Statistik wird **alle 14 Tage** erneuert (wie die
   Produktion nach jedem Scan), und war das Ziel erst später da, zählt der damals existierende Elternordner.
2. **Ziele.** Statt „Ordner Top-1 ≥ 65 % / Top-3 ≥ 85 %" gelten jetzt „richtiger Zweig" und „`sicher_bis` korrekt".
   Die Zielzahl für den Zweig wird festgelegt, wenn man sieht, was die Oberfläche in der Praxis daraus macht.
3. **Vorgänger (§6.4).** Der Auftrag macht den Vorgänger-Ordner zu Top-1 mit sehr hoher Konfidenz. Gemessen: nur
   **3 von 117** strikten Vorgängern lagen im Zielordner. Bei Strut sind gleichnamige Dateien meist Kopien
   (Mail-Anhänge, derselbe Plan als Versand und als Planstand, je Planstand ein neuer Ordner). Jeder Bonus verschlechterte
   den Treffer (Zweig unter 3 Optionen: 47 % → 41 % → 33 % → 17 %). **Standard: Bonus 0.** Geblieben ist die Rückfrage
   nach Fabios Vorgabe, aber an den **gewählten** Ordner gebunden, nicht als Ordnervorhersage.
4. **Rückfrage statt Häkchen.** Statt „Vorgänger archivieren" (ja/nein) gibt es `vorgaenger_modus`: archivieren,
   überschreiben (nur bei exakt gleichem Namen, atomar) oder beide behalten (`(2)`). So hat Fabio es beschrieben.
5. **Optionen sind Zweige, keine Blätter.** Die 2–3 wahrscheinlichsten Zweige unter `sicher_bis`, jeweils so tief
   verfeinert, wie ein Unterordner dominiert. `sicher_bis` ist der tiefste Knoten mit Summe ≥ Schwelle im ganzen Baum,
   nicht nur auf der Kette des besten Blatts.
6. **Scoring (§6.5).** Back-off nicht flach, sondern hierarchisch (Ordner → Teilbaum des Elternordners → Slot → Rolle
   → global). Neu hinzugekommen und nicht im Auftrag: Teilbaum-Rückhalt, Deckel für das idf-Gewicht (seltene Merkmale
   überstimmten sonst alles), ein **Namens-Signal** (Dateiname und Ordnername teilen Wörter, auch in Wortzusammensetzungen)
   und eine Temperatur, die an der Messung kalibriert wird. „Eindeutig" aus der Statistik nur, wenn der Ordner selbst
   genug Vorgeschichte hat.
7. **Merkmale (§6.1).**
   - Projektnummer steht bei Strut oft **nach dem Datum** (`260320_215_WB.vwx`); der Auftrag nennt nur „führend".
   - Neu: SIA-Phasenzahl und Plannummer (`182_51_202 …`).
   - Geschoss nur mit Plan-Kontext (nicht bei Word/Excel).
   - Gelernte BKP-Wörter: ein Code gilt nicht, wenn er die Projektnummer des Projekts ist.
   - Konfigurationsschlüssel: `ablage.dateiname_projektnummer` für den Dateinamen, `ablage.projektnummer_regex` für
     Projektnamen. Der Auftrag nennt einen Schlüssel für beides.
8. **Normalisierung (§5.1).** Zusätzlich: Ordner wie `182_51_2 Geschosse` (Projektnummer, Phase, Plannummer) zerlegen in
   Präfix `2` und Label `geschosse`.
9. **Ordnererfassung (§5.2).** `datei_anzahl` und `letzte_aenderung` kommen aus dem Walk (vorhandenes `stat()`), nicht per
   SQL aus `documents`. Das spart Zugriffe. Re-Scans schreiben unveränderte Ordner nicht neu.
10. **Mails.** Mails haben in der DB keine Pfade, sie fliessen in Projekt-Signale (Absender-Domain), nicht in die
    Ordner-Statistik.
11. **Neue Ordner** (virtuelle Vorlage-Slots) werden nur eine Ebene tief angelegt, und der Elternordner muss existieren.
12. **Zusätze, die der Auftrag nicht verlangt:** Rückgängig auch am Server-Mac, Prüfung gleichnamiger Dateien für jeden
    gewählten Ordner (auch ungescannte), Mehrfach-Drop gleicher Stamm, Kontext-Cache mit Vorwärmen beim Serverstart.
13. **Schema.** `ablage_vorgang` hat eine Zusatzspalte `lokal_src` (Drop am Server-Mac); `ablage_ordner.codes` hat den
    Default `[]`.

---

## 4. Was im Auftrag unklar oder nicht zutreffend war (herausgefunden)

- **Fixture-Kodierung.** Die Datei ist NFC, der Auftrag sagt NFD. Die NFD-Fälle entstehen jetzt im Test.
- **Musterordner.** Der echte Ordner auf dem NAS hat 839 Ordner (Fixture: 794). Die 45 Zusätzlichen sind Trenner
  (`00---------`), die der Import ignoriert. Pfad: `…/Office/Ordner 1_Vorlagen/000 Musterordner Objekt`.
- **Dev-DB.** Die Dev-DB im Repo ist nicht Strut (3 Projekte, 790 Dateien). Erst die Sicherung vom 27.09. (54 Projekte,
  351'939 Dokumente) lieferte belastbare Zahlen.
- **„Struktur vollständig bekannt"** ist für die Messung ungeeignet (siehe 3.1).
- **Vorgänger ⇒ gleicher Ordner** stimmt bei Strut nicht (siehe 3.3).
- **Plan-Namenskonvention.** Fabio: nicht konsistent, je Projekt anders. Das Bushof-Beispiel
  `{prj}_{phase}_{plannr}_{name}` ist ein Muster, kein Standard. Der Auftrag vermutete eine Excel-Beschreibung der
  Struktur; die gibt es nicht.
- **BKP gegen Projektnummer.** Projekte 184–220 kollidieren mit BKP-Codes 2xx (Projekt 211 und BKP 211 Baumeister). Das
  erzeugte zuerst falsche „BKP"-Merkmale (`Schnitt.dxf` → `bkp:188`). Behoben, aber das Wörterbuch bleibt anfällig.
- **„Identisch ausser Datum" ist mehrdeutig.** Gemeint sein kann (a) das Datum im Dateinamen oder (b) das Änderungsdatum der
  Datei bei gleichem Namen. Umgesetzt: Rückfrage bei **gleichem Namen ausser Datum im Namen** (a, schliesst b ein).
  **Das sollte Fabio bestätigen.**
- **Beispiele im Auftrag** waren erfunden; die echten Namen sehen anders aus (`<Datum>_<Projektnr>_<Name>`, viele
  `MAIL.pdf`, `image001.png`, Pos.-Nummern).
- **§6.6 Mehrfach-Drop** ist knapp beschrieben; die Umsetzung (Projekt übernehmen, gemeinsamer Ordner bei gleichem Stamm)
  ist meine Auslegung.
- **Absicht-Optionen (§5.4/§6.5)** sind als Wörterbuch-Einträge umgesetzt (`absichten` im YAML). Ob die Benennung zu
  Strut passt, ist ungeprüft.

---

## 5. Wo es Probleme gibt

**Qualität (gemessen auf der Strut-Sicherung, 17 Projekte ab Nr. 184, 2000 Testdateien, spätere Hälfte, Statistik allein):**

| Kennzahl | Auftrag-Stichprobe | natürliche Verteilung | Ziel (alt) |
|---|---:|---:|---|
| Projekt Top-1 | 57 % | 51 % | ≥ 95 % |
| Ordner Top-1 (Blatt) | 8,8 % | 9,1 % | ≥ 65 % |
| Ziel liegt im Zweig einer der 3 Optionen | 36 % | 44 % | neu festzulegen |
| `sicher_bis` korrekt | 96,1 % | 95,5 % | ≥ 95 % ✓ |
| Laufzeit Ordner-Teil p95 | 6 ms | 6 ms | < 300 ms ✓ |
| Laufzeit Projekt p95 | 440 ms | 456 ms | < 300 ms ✗ |

- Die Hälfte der Dateien hat weder Projektnummer noch Namen im Dateinamen. Dann entscheidet ein schwacher Prior; das
  Projekt ist oft nicht erkennbar.
- Der Ordner ist bei den tiefen, datierten Strukturen kaum auf Blattebene vorhersagbar. Selbst ein einfacher
  „ähnlichster Dateiname"-Vergleich liegt schon auf Phasenebene schlechter (32 % gegenüber 43 %).
- **Projekt-Laufzeit:** Der Volltext-Rückfall braucht ≈ 0,45 s, er bringt aber ≈ 10 Prozentpunkte beim Projekt.
  Vereinbart: so lassen, Ziel „im Mittel < 1 s, insgesamt höchstens 3 s".
- **Die Basis ist klein:** nur 17 Projekte, Testzeitraum rund 3 Monate. Die Zahlen schwanken, und manche Teilmengen
  haben nur zweistellige Fallzahlen (z. B. „Ziel existierte schon": n ≈ 210).
- **Nicht getestet:** Helper auf einem echten Mitarbeiter-Mac (Menüleisten-Drop, „Im Finder zeigen", Zugriff von der
  Browserseite auf `localhost:44380`). Der Server-Mac-Weg und der Server/Bridge-Weg sind mit echtem HTTP geprüft.
- **Mail-Drop (.eml):** Fabio will, dass `.eml` abgelegt werden können. Heute wird die Datei hochgeladen und analysiert,
  aber Absender und Betreff werden nicht gelesen; die Mail-Merkmale (Domain, Betreff) bleiben beim Drop leer. **Offen.**
- **Archivordner in der Sperrliste:** Steht `z_Archiv` in `scanner.excluded_folders`, wird der Ordner als „ausgeschlossen"
  statt als Archiv erfasst; das wirkt sich auf „alten Stand archivieren" aus. Auf dem iMac zu prüfen.
- **Leere Struktur bis zum ersten Scan:** `ablage_ordner` füllt sich erst beim nächsten normalen Scan (keine
  Nachführung), und die Neuberechnung läuft nach „Alle scannen". Direkt nach dem Update gibt es noch keine Optionen.
- **Kontext im Speicher:** Ordner und Statistik werden gecacht (Dev: ≈ 10 s Aufbau bei 17 Projekten, ≈ 9 s Statistik).
  Auf dem iMac mit 54 Projekten ist Speicher und Ladezeit noch nicht gemessen.
- **Rückgängig:** Nach dem Rückgängig verschwindet der Index-Eintrag sofort, ein Scan in der Zwischenzeit kann ihn aber
  kurz wieder anlegen. Geringes Risiko, nicht abgesichert.

---

## 6. Was gut läuft

- Die Sicherheitsarchitektur: Token-only, Server prüft das Ziel, der Helper prüft nochmals, Trockenlauf, Tests dafür.
- Die Ehrlichkeit des Systems: „sicher bis hier" stimmt zu 96 %, statt falsch und sicher zu sein. Es meldet „unklar",
  wo es unklar ist (Projekt unklar: 3 Projekte zur Wahl).
- Das Messskript ist wiederverwendbar (nur lesend, Zeit-Trennung, Basiswert eingefroren, Kalibrierung auf Trainingshälfte,
  Fehlertypen, Akzeptanz-Tabelle). Es hat die falschen Annahmen aufgedeckt, bevor sie in den Code gewandert wären.
- Die Ablage-Seite ist im Browser geprüft (Optionen, Tastatur, Mehrfach-Drop, Ablegen, Rückgängig).
- Nichts geht verloren: keine automatische Löschung; Überschreiben nur ausdrücklich und atomar; Namenskollision ergibt
  `(2)`; Quelle und Staging werden sauber getrennt.
- Der Vorschlagspfad ist schnell (6 ms für den Ordner-Teil) und der Scan kaum langsamer (+2,7 %).
- 785 Tests, einschliesslich echtem HTTP gegen die Bridge und die App.

---

## 7. Offene Punkte / Entscheidungen für Fabio

1. „Identisch ausser Datum": Namensdatum oder Änderungsdatum? (siehe Abschnitt 4)
2. Zielzahl für „richtiger Zweig" festlegen (aktuell 36–44 %).
3. `.eml` über den Drop: Absender und Betreff lesen lassen?
4. Projekt-Laufzeit: so lassen (≈ 0,45 s) oder Volltext-Rückfall kürzen (Wörter 40 → 15)?
5. Steht `z_Archiv` auf dem iMac in der Sperrliste?
6. Ist der Helper auf einem Mitarbeiter-Mac testbar (Menüleisten-Drop, Finder, Rückgängig)?

## 8. Stand der Etappen

| Etappe | Inhalt | Stand |
|---|---|---|
| 1 | Normalisierung | fertig |
| 2 | Ordner-Erfassung im Scan, Migrationen 030/031 | fertig, +2,7 % |
| 3 | Vorlage (Weg A/B), Einstellungen „Ablage-Struktur" | fertig |
| 4 | Merkmale, Wörterbuch, Statistik, Messskript, Basiswert | fertig |
| 5 | Scoring, Hierarchie, Vorstufen | fertig, Ziele verfehlt (siehe 5) |
| 6 | Transport (Helper, Staging, Bridge, Server-Mac) | fertig |
| 7 | Ablage-Seite, Mehrfach-Drop, Rückgängig | fertig |
| 8 | Lernen: Protokoll, inkrementelle Statistik, Regel-Vorschläge | offen |
| 9 | Abschluss: PROJEKT_STATUS, Config-Beispiel, Versionen (Server und Helper), nicht mergen | offen (Helper bereits 3.3.0) |
