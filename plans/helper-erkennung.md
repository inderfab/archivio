# Plan: Helper-Erkennung in der Weboberfläche

Stand: 2026-10-23, mit Fabio abgesprochen. Umsetzung nach dem Umzug des Codes ins private Repo.

## Ziel

Wer Archivio im Browser öffnet, sieht nur Knöpfe, die auf diesem Gerät auch funktionieren.
Fehlt der Archivio Helper, erscheint statt «Öffnen» und «Im Finder zeigen» ein klarer Weg
zur Installation. Mails öffnen sich nie mehr unbemerkt auf dem Server-Mac.

## Heutiges Verhalten (Ausgangslage)

- `web/templates/base.html`, `helperOpen()` / `helperReveal()`: rufen
  `http://localhost:44380/open|reveal` auf dem eigenen Gerät auf. Ohne Helper erscheint
  `alert('Archivio Helper läuft nicht. Bitte starten.')`. Irreführend für alle, die den
  Helper nie installiert haben.
- `openMail()` (ebenda): ohne Helper Ersatzweg `POST /api/open/mail/{id}`, der die Mail
  **auf dem Server-Mac** öffnet, egal wo der Browser läuft. Sitzt jemand an einem anderen
  Gerät, öffnet sich die Mail unbemerkt auf dem Bildschirm des Server-Macs.
- Auf iPhone, iPad und Windows gibt es keinen Helper: dieselben Fehlermeldungen,
  Archivio-Links (`localhost:44380/link?...`) öffnen sich dort nicht.
- Funktioniert ohne Helper: Suche, Trefferliste, Textvorschau, Foto-Galerie inkl.
  Vollbild (`/foto/{id}/ansicht`), PDF zusammenfügen.
- Hinweis: Der Helper-Port 44380 wird auf dem Server-Mac auch von der Server-Menüleisten-App
  bedient (`shared/menubar_bridge.py`). Der Server-Mac gilt deshalb als «Helper vorhanden».

## Fälle

| Gerät | Erkennung | Anzeige bei Dateien | Anzeige bei Mails |
|---|---|---|---|
| Mac mit Helper (oder Server-Mac) | `GET localhost:44380/ping` antwortet 200 | «Öffnen», «Im Finder zeigen» wie bisher | «In Apple Mail öffnen» wie bisher |
| Mac ohne Helper | Ping schlägt fehl, Gerät ist ein Mac | Ein Knopf «Helper installieren, um Dateien direkt zu öffnen» → `/helper` | derselbe Knopf |
| iPhone, iPad, Windows, andere | Gerät ist kein Mac | Kurzer Hinweis «Dateien öffnen geht nur auf einem Mac mit Archivio Helper.» (Variante 1) | derselbe Hinweis |

Mac-Erkennung: `navigator.userAgent` enthält `Macintosh` **und** `navigator.maxTouchPoints`
ist 0 (iPadOS meldet sich sonst ebenfalls als Macintosh).

## Umsetzung

1. **Erkennung (`web/templates/base.html`)**
   - Beim Laden einmal `fetch(HELPER + '/ping')` mit Zeitlimit von ca. 800 ms.
   - Ergebnis in `sessionStorage` merken (try/catch, falls Speicher blockiert), damit nicht
     jede Seite neu prüft. Bei Fehlschlag nach 60 s erneut prüfen, falls jemand den Helper
     inzwischen gestartet hat.
   - Ergebnis als Klasse auf `<body>`: `helper-ok`, `helper-missing` (Mac ohne Helper) oder
     `helper-unsupported` (kein Mac).
   - Bis das Ergebnis da ist: Knöpfe wie bisher zeigen (der Normalfall im Büro ist «Helper
     vorhanden», so flackert es dort nicht).
   - Auch HTMX-Nachladungen (Trefferliste, Vorschau) müssen die Klasse berücksichtigen. Das
     geht automatisch, wenn das Ein-/Ausblenden rein über CSS-Klassen am `<body>` läuft.

2. **Knöpfe in den Vorlagen**
   - Alle Stellen mit `helperOpen`, `helperReveal`, `openMail` bekommen eine gemeinsame
     Klasse (z. B. `needs-helper`). Betroffen: `search_results.html`, `index.html`
     (Kontextmenü, Lupe), `_preview.html`, `verlauf.html`, `_norms_row.html`,
     `_problem_docs.html`, `_problem_doc_files.html`.
   - Daneben je ein Platzhalter `helper-fallback`, der nur bei `helper-missing` bzw.
     `helper-unsupported` sichtbar ist. Eine gemeinsame Jinja-Makro-Datei verhindert, dass
     der Text an sieben Stellen gepflegt werden muss.
   - Texte:
     - Mac ohne Helper: Knopf «Helper installieren, um Dateien direkt zu öffnen» (Link auf
       `/helper`, dort liegt der Download bereit).
     - Kein Mac: Hinweis «Dateien öffnen geht nur auf einem Mac mit Archivio Helper.»
   - Die `alert()`-Meldungen in `helperOpen`/`helperReveal` bleiben als Rückfall, aber mit
     verständlichem Text und Hinweis auf die Helper-Seite.

3. **Mail-Ersatzweg absichern (`web/api.py`, `/api/open/mail/{id}`)**
   - Nur noch ausführen, wenn die Anfrage vom Server-Mac selbst kommt
     (`request.client.host` in `127.0.0.1`, `::1`). Sonst 403 mit verständlicher Meldung.
   - `openMail()` ruft den Ersatzweg nur noch auf, wenn der Ping erfolgreich war, aber
     `/open` für die Mail scheiterte (Sonderfall), nicht mehr bei fehlendem Helper.

4. **Tests**
   - Server: `/api/open/mail/{id}` von fremder Adresse → 403, von `127.0.0.1` → wird
     ausgeführt (Aufruf von `open` gemockt).
   - Vorlagen: Trefferliste und Vorschau enthalten Knöpfe mit `needs-helper` **und** den
     `helper-fallback`-Platzhalter mit Link auf `/helper`.
   - JavaScript-Logik der Geräte-Erkennung als reine Funktion, mit einem kleinen
     Playwright-Test für die drei Fälle (Helper da, Mac ohne Helper, iPhone-User-Agent).

## Später (notiert, nicht Teil dieser Umsetzung)

**Variante 2: «Datei anzeigen» über den Server** für iPhone, iPad und Windows.
Der Server liefert die Originaldatei direkt an den Browser, damit sich PDFs und Bilder auf
Geräten ohne Helper öffnen. Bewusst zurückgestellt, weil dann jedes Gerät im Büronetz ganze
Dateien holen kann, auch ohne eigenen NAS-Zugang. Falls umgesetzt:
- nur PDFs und Bilder,
- Sperrliste berücksichtigen,
- in den Einstellungen abschaltbar, Standard aus,
- Zugriffe im Verlauf protokollieren.

**Archivio-Links auf Geräten ohne Helper** (`localhost:44380/link?...`) öffnen sich dort
nicht. Lösbar erst zusammen mit Variante 2 (Link über den Server statt über den Helper).
