# Abschlussbericht Datei-Drop mit Ablage-Vorschlag (Etappen 1–9, mit Nachtrag 1)

Branch `feat/ablage-vorschlag` (von `feat/datei-drop`). **Nicht gemergt, nichts gepusht** (Fabio testet zuerst, siehe
PROJEKT_STATUS §4). Server 3.8.0, Helper 3.3.0. Technische Übergabe: `PROJEKT_STATUS.md` §19.

---

## 1. Stand

| Etappe | Inhalt | Stand |
|---|---|---|
| 1–4 | Normalisierung, Ordnererfassung, Vorlage, Merkmale, Statistik, Messskript | fertig |
| 5 | Scoring, Hierarchie, Vorstufen | fertig; Ziele des Auftrags verfehlt, Ziele umgestellt (Nachtrag 1) |
| 6 | Transport (Helper, Server-Mac, Bridge nur mit Token) | fertig |
| 7 | Ablage-Seite, Mehrfach-Drop, Rückgängig | fertig |
| Nachtrag §2 | Messung: Tiefe, Schwellenkurve, Projekt-Kennzahlen, drop-typische Ereignisse, Hinweiszeile | fertig, `planung/datei-drop-ablage_messung-nachtrag-1.md` |
| Nachtrag §4 | `.eml`, `ausgeschlossen` als Spalte, Erst-Erfassung nach Update, nicht blockierender Cache, Diagnose | fertig |
| Nachtrag §3 | Ordnersuche, zuletzt verwendet, neuer datierter Ordner | fertig |
| 8 (reduziert) | `ablage_log` mit Quelle und Zeit, inkrementelle Statistik, Auswertungsskript | fertig; **Regel-Vorschläge bewusst zurückgestellt** |
| 9 | PROJEKT_STATUS, Config-Beispiel, Versionen, Testanleitung | fertig, dieser Bericht |

Tests: 829 grün (`pytest tests/ -q --ignore=tests/test_search_recall.py`).

## 2. Was sich seit dem Kommentar geändert hat

- **`schwelle_sicher` 0.80 → 0.95.** Die Kurve Abdeckung gegen Fehler zeigt: Aussagen ab „Bereich“ sind bei jeder Schwelle zu 55–78 %
  falsch. Gewählt ist die strengste Schwelle (die Regel „Fehler ≤ 5 %“ wird nirgends erreicht). **Nebenwirkung:** die Schwelle bestimmt auch, unter welchem Knoten
  die Optionen gebildet werden; nachgemessen verbessert sie den Zweig bei drop-typischen Ereignissen (43 → 49 %), verschlechtert ihn bei allen Dateien (44 → 37 %).
- **Hinweiszeile für Vorgänger: nicht umgesetzt.** Zuwachs höchstens 0,7 Prozentpunkte.
- **`ausgeschlossen`** ist eine eigene Spalte; ein ausgeschlossenes `z_Archiv` bleibt ein Archiv-Ordner.
- **`.eml`** werden beim Drop gelesen (Absender, Betreff, Datum, mit der vorhandenen Mail-Logik). **`.msg` nicht** (offen).
- **Erst-Erfassung nach dem Update**: einmal pro Projekt im Hintergrund, nur Ordner (keine Datei wird gelesen).
- **Seite bei kaltem Cache**: „Vorschläge werden vorbereitet…“ mit Ordnerbrowser, dann Nachladen. Der Drop wartet höchstens ~2 s.

## 3. Ergebnis der Messung (Kurzfassung)

Strut-Sicherung 27.09.2026, 17 Projekte ab Nr. 184. Statistik allein, rollende Statistik, spätere Hälfte.

- Projekt Top-1 51–61 %, **Top-3 59–67 %** (kaum besser als Top-1: das Problem sind die Projektsignale, nicht das Ranking).
  Mit Projektsignal im Namen 82 %, ohne 50 %.
- Ordner auf Blattebene 8–14 %; **richtiger Zweig in den 3 Optionen 37–49 %** (nach der Schwellen-Umstellung auf 0.95).
- Aussagen tiefer als „Projekt“ sind nicht verlässlich (Fehler 55–78 %). Die Oberfläche stellt deshalb nur das Projekt als „sicher“ dar.
- **Neuer datierter Ordner** („Neuer Ordner `<JJMMTT>_…`“): erscheint in 47–57 % der Fälle, trifft den richtigen Zweig in 9–16 % (die Obergrenze, dass das
  wahre Ziel überhaupt ein neuer datierter Ordner ist, liegt bei 32–49 %). Mit dieser Karte steht der richtige Zweig in rund 39 % (alle Dateien) bis
  50 % (drop-typische Ereignisse) der Fälle bereit.

Das ist der Grund, warum die Oberfläche jetzt auf **schnelles manuelles Finden** setzt: Suchfeld, „Zuletzt verwendet“, „Neuer Ordner“.

## 4. Was Fabio jetzt tun muss

1. **Testen** (Anleitung unten, 2–3 Wochen im Alltag).
2. **Nach 2–3 Wochen auswerten:** `scripts/ablage_log_auswertung.py` (siehe 6). Erst danach wird über weiteres Scoring und Regel-Vorschläge entschieden.
3. **Entscheiden**: „Identisch ausser Datum“ = Datum im Dateinamen (so umgesetzt; Empfehlung im Nachtrag: bestätigen).
4. Auf dem iMac nach dem ersten Start im Log prüfen: `Ablage: Erst-Erfassung abgeschlossen: … Projekte, … Ordner, … s`
   und im Diagnose-Endpunkt die Zeile „Ablage-Cache“ (Grösse, Ladezeit).
5. **Musterordner** in Einstellungen → Ablage-Struktur eintragen
   (`…/Office/Ordner 1_Vorlagen/000 Musterordner Objekt`, 839 Ordner) und „Lernen ab Projektnummer“ 184 setzen.

## 5. Testanleitung Helper auf einem Mitarbeiter-Mac (10 Schritte)

Voraussetzung: Server 3.8.0 läuft, Helper 3.3.0 ist installiert und zeigt auf den Server; mindestens ein Projekt ist gescannt
(Einstellungen → Ablage-Struktur zeigt „Erkannte Struktur“). Testdateien: eine PDF mit Projektnummer im Namen (z. B.
`211_Test Protokoll.pdf`), eine PDF ohne Nummer, eine `.eml`, eine `.dwg`. **Zuerst mit Trockenlauf**, dann echt.

0. **Trockenlauf einschalten** (Terminal auf diesem Mac, dann Helper neu starten):
   ```bash
   launchctl setenv ARCHIVIO_UPLOAD_DRY_RUN 1
   ```
   Abschalten für die echten Schritte: `launchctl unsetenv ARCHIVIO_UPLOAD_DRY_RUN` und Helper neu starten.
1. **Eine Datei droppen.** `211_Test Protokoll.pdf` aufs Archivio-Symbol in der Menüleiste ziehen. Erwartet: der Browser öffnet sich mit
   der Ablage-Seite, Projekt „211 …“ mit Badge, 1–3 Optionen mit Gründen, Karte 1 vorgewählt. Mit `1`/`2`/`3` die Optionen wechseln.
2. **Trockenlauf prüfen.** Enter. Erwartet: „🧪 TROCKENLAUF — nichts wurde verschoben“, die Datei liegt noch auf dem Schreibtisch.
3. **Echt ablegen.** Trockenlauf abschalten (Schritt 0), Helper neu starten, nochmals droppen, Enter. Erwartet: „✓ abgelegt in …“, die Datei
   ist weg vom Schreibtisch und im Projektordner; nach wenigen Sekunden in Archivio auffindbar (Suche nach dem Dateinamen).
4. **Finder.** Auf der Ergebnis-Zeile „Im Finder zeigen“ klicken. Erwartet: der Finder öffnet den Ordner mit der Datei.
5. **Rückgängig.** Direkt nach dem Ablegen (innerhalb von 5 Minuten) „Rückgängig“ klicken. Erwartet: die Datei liegt wieder auf dem
   Schreibtisch und ist im Ziel weg. Nochmals versuchen mit „Kopie am ursprünglichen Ort behalten“ angehakt: dann gibt es kein
   Rückgängig. Nach 5 Minuten verschwindet der Link.
6. **Mehrere Dateien.** Drei Dateien gleichzeitig droppen (eine mit Projektnummer, eine ohne, die `.eml`). Erwartet: **ein** Browser-Tab mit
   drei Zeilen; die Datei ohne Nummer übernimmt das Projekt der ersten; „Alle wie erste“ setzt dasselbe Ziel; Enter legt alle ab.
7. **Gleichnamige Datei.** Dieselbe Datei nochmals (anderer Inhalt, gleicher Name) in denselben Ordner legen. Erwartet: die Rückfrage
   „Gibt schon ein Dokument mit diesem Namen …“ mit „Alten Stand nach z_Archiv schieben“ / „Überschreiben“ / „Beide behalten“;
   „Ablegen“ bleibt gesperrt, bis gewählt ist. Mit „Archivieren“ prüfen, dass der alte Stand in `z_Archiv` liegt.
8. **Suche, zuletzt, neuer Ordner.** Ins Suchfeld „fassade“ tippen: die Treffer filtern sofort (Pfeiltasten und Enter wählen). Eine zweite Datei
   droppen: die Zeile „Zuletzt verwendet“ zeigt die letzten Ziele dieses Macs. In einem Zweig mit datierten Unterordnern erscheint „Neuer Ordner“
   mit Datum und editierbarem Thema.
9. **Server nicht erreichbar.** WLAN/Netzwerk trennen (oder den Server stoppen) und droppen. Erwartet: eine Mitteilung „Ablage nicht möglich“
   statt eines leeren Browser-Tabs. Netz wieder verbinden; der nächste Drop funktioniert ohne Neustart des Helpers.
10. **Helper ohne Netz / nicht gestartet.** Beim Ablegen den Helper beenden: Erwartet: „Archivio Helper läuft nicht auf diesem Mac. Bitte starten
    und nochmals versuchen.“ Nach dem Start des Helpers genügt ein Klick auf „Ablegen“. Zum Schluss mit der `.dwg` prüfen, dass auch CAD-Dateien
    (nur Name, Grösse, Hash) einen Vorschlag bekommen und sich ablegen lassen.

Ergebnisse bitte kurz festhalten (hat funktioniert / Fehlermeldung / Beobachtung); Auffälligkeiten stehen im Server-Log
(`~/Library/Application Support/Archivio/logs/server.log`) und im Helper-Log (`~/Library/Logs/ArchivioHelper.log`).

## 6. Auswertung nach 2–3 Wochen

```bash
ARCHIVIO_DB="$HOME/Library/Application Support/Archivio/archivio.db" \
  "/Applications/Archivio Server.app/Contents/Resources/archivio-python-x86_64/bin/python3" \
  "/Applications/Archivio Server.app/Contents/Resources/scripts/ablage_log_auswertung.py" --seit 2026-10-15
```
(Auf dem Dev-Mac: `.venv/bin/python scripts/ablage_log_auswertung.py`. Das Skript liegt im Server-Bundle unter `Contents/Resources/scripts/`.)

Der Bericht zeigt: Anteil der Wege (Option / zuletzt / Suche / Browser / neuer Ordner), Median und 90. Perzentil der Zeit von Seitenaufruf
bis Ablegen, die Rangverteilung der gewählten Option, wie oft das Projekt stimmte. Leitfragen für den Entscheid:

- Wird die Option (Rang 1–3) oft genug gewählt, um Scoring-Tuning zu rechtfertigen, oder nimmt man überwiegend Suche/Browser?
- Ist die Zeit bis Ablegen kürzer als selbst suchen (Gefühl gegen Median)?
- Stimmt das Projekt? (sonst Projektsignale verbessern, nicht den Ordner-Rang)

## 7. Offen

- `.msg` (Outlook) wird beim Drop nicht gelesen.
- Helper auf einem echten Mac nicht getestet (Anleitung oben).
- Regel-Vorschläge (`ablage_regel`) zurückgestellt.
- Rückgängig gegen einen Scan in den fünf Minuten: bekannte Grenze (PROJEKT_STATUS §19).
