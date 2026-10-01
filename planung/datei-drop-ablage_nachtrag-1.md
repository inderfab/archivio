# Nachtrag 1 zum Auftrag Datei-Drop: Rückmeldung zum Stand nach Etappe 7

> **Für Claude Code.** Antwort auf `datei-drop-ablage_kommentar.md`. Ablage im Repo:
> `planung/datei-drop-ablage_nachtrag-1.md`. Gilt zusätzlich zum Auftrag. Wo sich beide
> widersprechen, gilt dieser Nachtrag.
>
> Stand: 2026-10-01

---

## 1. Übernommen: deine Abweichungen sind richtig

Folgende Abweichungen vom Auftrag sind bestätigt und gelten ab jetzt als Auftrag:

- Abschnitt 3, Punkte 1–13
- Messung mit der Struktur zum jeweiligen Zeitpunkt
- Ziel „richtiger Zweig“ statt „richtiges Blatt“
- Vorgänger-Bonus 0
- `vorgaenger_modus`
- Zweige als Optionen
- hierarchischer Back-off
- Namens-Signal
- Temperatur
- Projektnummer nach dem Datum

**Korrekturen am Auftrag selbst:**

- **NFD/NFC:** Die ZIP vom Mac war NFD. Die Fixture habe ich vor der Übergabe nach NFC
  umgewandelt. Der Auftrag hat beides vermischt. Dass die NFD-Fälle jetzt im Test erzeugt
  werden, ist die richtige Lösung.
- **Excel zur Struktur:** Die vermutete Excel-Beschreibung gibt es nicht. Erledigt.
- **Vorgänger:** Die Annahme „Vorgänger ⇒ gleicher Ordner“ war falsch. Das ist bei Strut nicht
  so, deine Messung belegt es.

---

## 2. Messung: vor weiterem Tuning ergänzen

Die heutigen Zahlen sind ehrlich, messen aber zum Teil etwas anderes als den echten Drop. Bitte
die folgenden vier Punkte im Messskript ergänzen. Sie gehören in den Bericht, ohne neues
Scoring.

### 2.1 `sicher_bis` ohne Tiefe ist kein Qualitätsmass

96 % korrekt lassen sich trivial erreichen, indem man nichts behauptet. Bei Projekt Top-1 von
51–57 % ist `sicher_bis` vermutlich oft nur die Basis oder das Projekt. Zu ergänzen sind:

- **die Verteilung der Tiefe von `sicher_bis`:** keine Aussage / Projekt / Bereich (`a_`…`h_`,
  Phase) / tiefer, jeweils mit der Trefferquote pro Stufe;
- **eine Kurve Abdeckung gegen Fehler** für `schwelle_sicher` von 0.60 bis 0.95: Anteil der
  Fälle mit Aussage mindestens auf Projektebene, dazu der Anteil falscher Aussagen;
- die Wahl der Schwelle anhand dieser Kurve, dokumentiert im Bericht.

### 2.2 Projekt-Kennzahlen vervollständigen

Ergänzen: **Projekt Top-3**, der Anteil „Projekt eindeutig“ und davon der Anteil falsch. Wenn
Top-3 hoch ist, genügt im Alltag ein Klick und das Problem ist klein. Ist Top-3 ebenfalls
niedrig, liegt das Problem bei den Projektsignalen.

### 2.3 Testmenge: drop-typische Ereignisse statt aller Dateien

Die Stichprobe enthält Dateien, die nie über einen Drop ankommen: CAD-Exporte, die ArchiCAD
direkt schreibt, Foto-Serien, Dutzende `image001.png`. Zusätzlich zur heutigen Menge
(nicht stattdessen) diese Variante berichten:

- **Ereignisse statt Dateien:** gleicher Zielordner, gleicher Tag und gleiche Endung zählen
  als **ein** Ablage-Ereignis. Bewertet wird ein Vertreter (die erste Datei). Ein Export von
  60 PDFs darf nicht 60-mal zählen.
- **Drop-typische Endungen:** `.pdf .docx .doc .xlsx .eml .msg .jpg .png .zip`.
  Ausgeschlossen werden `image\d{3}\.(png|jpg)` und CAD-/BIM-Formate.
- Pro Teilmenge zusätzlich die Aufteilung **„Name trägt ein Projektsignal (Nummer oder Name)
  ja/nein“**. Das zeigt die Obergrenze der Projekterkennung.

### 2.4 Variante: Vorgänger als Hinweis, nicht als Rang

Gleichnamige frühere Versionen nicht in die Rangfolge einrechnen. Sie erscheinen als
**eigene Hinweiszeile** („Frühere Version liegt in …“) zusätzlich zu den 3 Optionen und
belegen keinen Optionsplatz. Gemessen wird: Anteil, in dem der wahre Zweig unter den
3 Optionen **oder** in der Hinweiszeile liegt. Wenn das deutlich hilft, wird es in die Ablage-
Seite übernommen.

---

## 3. Oberfläche: bei ca. 40 % Zweig-Treffer muss die manuelle Suche schnell sein

Das Ziel lautet „schneller als selbst suchen“, nicht „immer richtig raten“. Das sind die drei
grössten Hebel. Umsetzen in dieser Reihenfolge:

1. **Ordnersuche im Projekt (Tippen filtert).** Ein Eingabefeld über den Optionen. Tippen
   (zum Beispiel „fassade“ oder „bauing“) filtert sofort die `ablage_ordner` des gewählten
   Projekts, über `label` und `name`, auch Teilwörter. Das läuft aus der DB, nicht über das
   NAS, und braucht keinen Netzwerkzugriff. Treffer stehen als Liste mit Pfad ab Projekt;
   Pfeiltasten und Enter wählen aus. Danach wird `gewaehlt_rang = 0` geloggt, mit Kennzeichen
   „suche“.
2. **„Zuletzt verwendet“ von diesem Rechner.** Die letzten 5 Ziele dieses `host` aus den
   letzten 14 Tagen, zuerst im vorgeschlagenen Projekt, als eigene Zeile. Gleichzeitig wird es
   ein Signal im Scoring, falls die Messung im Alltag (§5) zeigt, dass es trägt.
3. **Datierte Sammelordner: neuen Ordner vorschlagen.** Sind die Unterordner eines Zweigs
   mehrheitlich datiert (zum Beispiel ≥ 60 % mit `^\d{6}[_ ]` wie `260721_Thema`), dann gibt
   es dort die Option **„Neuer Ordner `<JJMMTT>_…`“**. Das Datumsformat wird von den
   Geschwisterordnern übernommen, das Thema ist ein vorausgefülltes Feld (Vorschlag aus den
   Dateinamen-Tokens, editierbar). Das adressiert direkt die 9 % auf Blattebene: Das richtige
   Blatt gab es oft noch gar nicht.
   In der Messung zählt ein solcher Fall als Treffer, wenn das wahre Ziel ein neuer datierter
   Ordner unter dem vorgeschlagenen Zweig ist. Separat ausweisen.

---

## 4. Entscheide zu den offenen Punkten (Abschnitt 7 des Kommentars)

Wo „Fabio“ steht, ist das nur eine Empfehlung von mir. Er entscheidet.

| # | Punkt | Entscheid / Empfehlung |
|---|---|---|
| 1 | „Identisch ausser Datum“ | Empfehlung: **(a) Datum im Dateinamen**, wie umgesetzt. Gleicher Name mit anderem Änderungsdatum ist ohnehin der Kollisionsfall. **Fabio bestätigt.** |
| 2 | Zielzahl Zweig | Erst nach §2.3 und §5 festlegen. Vorläufig gilt für drop-typische Ereignisse: Zweig in den 3 Optionen ≥ 50 %, sicher-falsch ≤ 3 %, Projekt Top-3 ≥ 85 %. |
| 3 | `.eml` | **Ja.** Absender, Betreff und Datum mit der vorhandenen Mail-Logik (`scanner/mail_scanner.py`) parsen und in die Merkmale `dom:`/`tok:` geben. Kein neuer Parser. `.msg` (Outlook) nur, wenn der vorhandene Code es schon kann, sonst als offener Punkt notieren. |
| 4 | Projekt-Laufzeit | So lassen (ca. 0,45 s). Gilt: im Mittel < 1 s, höchstens 3 s. Wörter nicht kürzen, ausser die Messung zeigt, dass es nichts kostet. |
| 5 | `z_Archiv` in der Sperrliste | **Unabhängig vom iMac lösen:** Die Art eines Ordners und der Ausschluss sind zwei verschiedene Dinge. `ausgeschlossen` wird eine **eigene Spalte** (`INTEGER 0/1`), und `art` bleibt `archiv`, wenn das Label passt. „Alten Stand archivieren“ muss in ausgeschlossene Archivordner funktionieren (Datei wird verschoben, aber nicht indexiert). Neue Migration, Test dazu. |
| 6 | Helper auf Mitarbeiter-Mac | **Fabio.** Bitte eine Testanleitung mit 10 Schritten in den Abschlussbericht: Drop einer und mehrerer Dateien, Finder, Rückgängig, Trockenlauf, Server nicht erreichbar, Helper ohne Netz. |

**Weitere Punkte aus Abschnitt 5:**

- **Leere Struktur nach dem Update:** Nach Migration 030 einmalig im Hintergrund eine
  **reine Ordner-Erfassung** starten. Das ist ein `os.walk` nur über Ordner, ohne Dateien zu
  lesen, und läuft unter dem Scan-Lock sowie einmal pro Projekt. Danach wird die Statistik neu
  aufgebaut. So hat die Ablage-Seite direkt nach dem Update Optionen. Die Dauer auf dem NAS im
  Log ausweisen.
- **Speicher und Ladezeit auf dem iMac:** In den Diagnose-Endpunkt (`/api/debug/diagnostics`)
  aufnehmen: Grösse des Ablage-Caches (Einträge, geschätzte MB) und Ladezeit. Das Vorwärmen läuft
  im Hintergrund und darf den Start nicht blockieren. Ist der Cache noch nicht bereit, zeigt
  die Seite „Vorschläge werden vorbereitet…“ mit dem Ordnerbrowser und lädt dann nach.
- **Rückgängig gegen Scan:** Akzeptiert, wie es ist. Als bekannte Grenze in PROJEKT_STATUS
  vermerken.

---

## 5. Priorität ab jetzt: echte Nutzung messen statt weiter am Stellvertreter tunen

Die Messung auf historischen Daten ist ein Stellvertreter. Ob die Funktion im Alltag schneller
ist, zeigt erst `ablage_log`. Darum ist die Reihenfolge ab jetzt:

1. **§2 Messung ergänzen.** Nur berichten, kein Tuning.
2. **§4 Punkte 3 und 5 sowie die Ordner-Erfassung nach dem Update.**
3. **§3 Oberfläche:** Suche, zuletzt verwendet, neuer datierter Ordner.
4. **Etappe 8, reduziert:**
   - `ablage_log` vollständig, inklusive Kennzeichen „option / zuletzt / suche / browser /
     neuer_ordner“ und Zeit von Seitenaufruf bis Ablegen;
   - inkrementelle Statistik nach jeder Ablage.
   - **Regel-Vorschläge zurückstellen,** bis Daten aus dem Alltag da sind.
5. **Etappe 9** wie im Auftrag, plus die Testanleitung aus §4.6. Nicht mergen.
6. Danach testet Fabio 2–3 Wochen. Ausgewertet wird `ablage_log`: Anteil Option / zuletzt /
   Suche / Browser, Median der Zeit bis Ablage und Rang-Verteilung. Erst dann wird über weiteres
   Scoring-Tuning und Regel-Vorschläge entschieden.

**Nicht tun:** weitere Gewichte oder Signale gegen die historische Messung optimieren, solange
§5.6 nicht vorliegt. Bei nur 17 Projekten und rund 3 Monaten Testzeitraum besteht die Gefahr,
das Rauschen zu optimieren.
