# Messbericht zu Nachtrag 1 §2 (nur berichten, kein Tuning)

Stand: nach Etappe 7, Strut-Sicherung vom 27.09.2026 (17 Projekte ab Nr. 184, 91'539 Dateien, 4'899 Dateien ab dem
Stichtag 3.7.2026). Statistik allein, spätere Hälfte der Testdateien, Statistik-Stand alle 14 Tage erneuert. Rohberichte:
`/Users/fi/archivio-testdaten/nachtrag1_*.md` (Aufruf: `scripts/ablage_messung.py --testmenge alle|drop`).

Drei Läufe: Stichprobe nach Auftrag (gleichmässig je Top-Ordner), natürliche Verteilung (2000 Dateien) und **drop-typische
Ereignisse** (957 Ereignisse, siehe 3).

## 1. `sicher_bis` ohne Tiefe ist kein Qualitätsmass (§2.1) — bestätigt

Die früheren 96 % kamen daher, dass fast nie etwas Tiefes behauptet wurde.

Stufe von `sicher_bis` (natürliche Verteilung, alle Dateien, n = 932):

| Stufe | Anteil der Fälle | davon richtig |
|---|---:|---:|
| keine Aussage (Projekt unklar) | 46,7 % | – |
| Projekt | 45,9 % | 80,1 % |
| Bereich / Phase (Ebene 1) | **2,0 %** | **47 %** |
| tiefer (Ebene ≥ 2) | **5,4 %** | **24 %** |

Drop-typische Ereignisse (n = 458): keine Aussage 55 %, Projekt 36 % (70 % richtig), Bereich 3,7 % (53 % richtig),
tiefer 5,0 % (35 % richtig).

**Kurve Abdeckung gegen Fehler** (Schwelle `schwelle_sicher` 0.60 → 0.95, natürliche Verteilung):

| Schwelle | Aussage ≥ Bereich: Abdeckung / falsch | ≥ tiefer: Abdeckung / falsch |
|---:|---:|---:|
| 0.60 | 20,8 % / 77,8 % | 15,8 % / 80,3 % |
| 0.80 | 7,4 % / 69,6 % | 5,4 % / 76,0 % |
| 0.95 | 2,3 % / 61,9 % | 1,2 % / 72,7 % |

(Drop-Ereignisse: 17,7 % / 55,6 % bei 0.60 bis 3,1 % / 71,4 % bei 0.95.)

**Schwellenwahl, dokumentiert:** Regel = grösste Abdeckung ab Stufe „Bereich“ bei Fehler ≤ 5 %. **Sie ist bei jeder
Schwelle verfehlt**, der Fehler liegt zwischen 55 % und 78 %. Die Schwelle trennt gute von schlechten Aussagen praktisch
nicht, die Wahrscheinlichkeiten unterhalb des Projekts sind nicht kalibriert. Gewählt ist deshalb die strengste Schwelle,
`schwelle_sicher = 0.95` (vorher 0.80): so wenige falsche „sicher bis hier“-Aussagen wie möglich. Das ändert die
Rangfolge der Optionen nicht, nur wie oft das System etwas als sicher behauptet.

**Folgerung für die Oberfläche:** Eine Aussage tiefer als „Projekt“ sollte nicht als Sicherheit dargestellt werden. Die
Optionen bleiben nützlich (richtiger Zweig in 43 %), aber nicht als „sicher“.

## 2. Projekt-Kennzahlen (§2.2)

| | alle Dateien (natürlich) | drop-typische Ereignisse |
|---|---:|---:|
| Projekt Top-1 | 51,4 % | 61,0 % |
| Projekt Top-3 | **59,0 %** | **67,2 %** |
| „sicher“ (p ≥ 0.6) | 50,4 %, davon falsch 21,2 % | 44,5 %, davon falsch 25,4 % |
| „eindeutig“ (p ≥ 0.95) | 47,7 %, davon falsch 19,1 % | 39,5 %, davon falsch 28,0 % |

**Top-3 ist kaum besser als Top-1** (+8 und +6 Punkte). Mit einem Klick auf eines von drei Projekten ist das Problem also
**nicht** klein: Es liegt bei den **Projektsignalen**, nicht beim Ranking. Selbst „eindeutige“ Projektaussagen sind zu
einem Fünftel bis einem Viertel falsch.

## 3. Drop-typische Ereignisse und Projektsignal (§2.3)

Testmenge: nur `.pdf .docx .doc .xlsx .eml .msg .jpg .png .zip`, ohne `imageNNN.png/jpg` und CAD/BIM; gleicher
Zielordner + Tag + Endung = ein Ereignis (Vertreter = erste Datei). Aus 4'899 Dateien werden 957 Ereignisse.

**Aufteilung „Dateiname trägt ein Projektsignal (Nummer oder Name)“ — die Obergrenze der Projekterkennung:**

| | JA | NEIN |
|---|---:|---:|
| Anteil der Ereignisse | 33 % (160 von 479) | 67 % (319) |
| Projekt Top-1 | **81,9 %** | 50,5 % |
| Projekt Top-3 | 81,9 % | 59,9 % |
| „sicher“, davon falsch | 98 %, davon 18,5 % falsch | 17,6 %, davon 44,6 % falsch |
| Ordner: richtiger Zweig in den 3 Optionen | 39,2 % | 44,9 % |

- **Auch mit Projektsignal ist die Obergrenze nur 82 %.** Die Nummer im Dateinamen führt in rund jedem fünften Fall zu
  einem anderen Projekt (Plan-Kopien anderer Projekte, Nummern von Fachplanern, Vorlagen).
- **Ohne Signal (zwei Drittel der Ereignisse) ist das Projekt in 82 % der Fälle „keine Aussage“**, die Trefferquote bei
  den wenigen Aussagen liegt unter 50 %. Dort ist Archivio auf den Inhalt und die Gewohnheiten angewiesen, nicht auf den Namen.

## 4. Vorgänger als Hinweiszeile statt als Rang (§2.4) — bringt kaum etwas

| | alle Dateien | Ereignisse |
|---|---:|---:|
| Fälle mit gleichnamiger früherer Version | 27,6 % | 16,2 % |
| Zweig in den 3 Optionen (heute) | 43,8 % | 43,0 % |
| … in den 3 Optionen **oder** in der Hinweiszeile | 44,0 % (+0,2) | 43,7 % (+0,7) |
| Hinweiszeile trifft den Zweig (bei Fällen mit Vorgänger) | 2,3 % | 10,8 % |
| Hinweisort ist Geschwisterordner des Ziels | 5,1 % | 14,9 % |

**Der Zuwachs ist ≤ 0,7 Prozentpunkte.** Es lohnt sich nicht, die Hinweiszeile in die Ablage-Seite zu übernehmen.
Auffällig ist nur der Geschwister-Fall (15 % bei Ereignissen): Der Vorgänger liegt oft im Nachbarordner, in dem das Ziel
neu entsteht — das deckt der Vorschlag „Neuer Ordner“ (§3.3 des Nachtrags) besser ab.

## 5. Was daraus folgt

1. Projektsignale verbessern, nicht den Ordner-Rang: mit Signal 82 %, ohne 50 %. Der Hebel sind Gewohnheiten (zuletzt
   benutzte Projekte dieses Rechners, bereits im Scoring als Prior vorhanden) und der Inhalt.
2. Die Oberfläche darf nur „Projekt“ als sicher darstellen, Zweige als Vorschlag.
3. Die Hinweiszeile für Vorgänger wird nicht umgesetzt.
4. Kein weiteres Scoring-Tuning gegen diese Messung (Nachtrag §5): erst `ablage_log` aus dem Alltag.

## 6. Nachgemessen nach der Umstellung auf `schwelle_sicher = 0.95` und mit „Neuer Ordner“

Gleiche Läufe wie oben, jetzt mit Schwelle 0.95 und dem Vorschlag „Neuer Ordner `<JJMMTT>_…`“ (Nachtrag §3.3).

| | alle Dateien (natürlich) | drop-typische Ereignisse |
|---|---:|---:|
| Ordner Top-1 (vorher → jetzt) | 9,1 → 13,7 % | 7,9 → 9,8 % |
| richtiger Zweig in den 3 Optionen (vorher → jetzt) | 43,8 → **37,0 %** | 43,0 → **49,1 %** |
| richtiger Zweig in Option 1 | 27,3 → 24,7 % | 24,7 → 29,3 % |
| Vorschlag „Neuer Ordner“ erscheint | 47 % | 57 % |
| wahres Ziel ist ein neuer datierter Ordner (Obergrenze) | 49 % | 32 % |
| **Treffer** „Neuer Ordner“ (neuer datierter Ordner unter dem vorgeschlagenen Zweig) | **16,1 %** | **9,2 %** |
| Zweig in den Optionen **oder** Treffer bei „Neuer Ordner“ | 38,9 % | 49,6 % |

- Die Schwelle verschiebt die Optionen: Für die **realistischere Testmenge (drop-typische Ereignisse) wird der Zweig besser (+6 Punkte)**, für alle
  Dateien schlechter (−7). Bei Ereignissen mit Projektsignal im Namen: 40,5 %, ohne: 53,4 %.
- „Neuer Ordner“ trifft in 9–16 % der Fälle, erscheint aber in 47–57 %: Die Karte ist oft nicht das, was man braucht (Präzision ca. 16–29 %), kostet
  aber nur eine Zeile. Wo das wahre Ziel ein neuer datierter Ordner ist (32–49 %), trifft sie in rund einem Drittel dieser Fälle den richtigen Zweig.
  Nach dem Alltagstest an `ablage_log` (Quelle „neuer_ordner“) prüfen, ob sie genutzt wird.
- Mehr als „kein Rückschritt“ lässt sich daraus nicht ableiten; das Rauschen bei 17 Projekten ist gross (Nachtrag §5: kein weiteres Tuning).
