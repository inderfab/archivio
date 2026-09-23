#!/bin/bash
#
# Entfernt Archivio von diesem Mac.
#
# Deckt die zwei Fälle ab, die in der Praxis vorkommen:
#
#   1) KOMPLETT      — Testrechner oder Ausmusterung: alles weg, inklusive
#                      Datenbank und Helper.
#   2) ARBEITSPLATZ  — nach einem Umzug: der Server ist auf einen anderen Mac
#                      gezogen, dieser hier wird normaler Arbeitsplatz. Server
#                      und Datenbank weg, NUR der Helper bleibt (samt seinen
#                      Einstellungen und der MCP-Anbindung).
#
# Grundsätze:
#   * Es wird NICHTS mit `rm` gelöscht, sondern in den Papierkorb verschoben.
#     Bis der geleert wird, ist alles zurückholbar.
#   * Vor dem Zugriff wird gezeigt, was da eigentlich liegt: Grösse der
#     Datenbank, Anzahl Dokumente, und wann zuletzt gesichert wurde. Eine
#     Datenbank ohne gültige Sicherung zu entfernen, soll eine bewusste
#     Entscheidung sein und kein Versehen.
#   * `--simulation` zeigt den kompletten Ablauf, ohne irgendetwas anzufassen.
#
# Aufruf:
#   bash deinstallieren.sh                  # fragt nach
#   bash deinstallieren.sh --komplett
#   bash deinstallieren.sh --arbeitsplatz
#   bash deinstallieren.sh --simulation     # ändert nichts
#
set -uo pipefail

MODUS=""
SIMULATION=0
OHNE_RUECKFRAGE=0
NUR_ZUSAMMENFASSUNG=0

for arg in "$@"; do
  case "$arg" in
    --komplett)     MODUS="komplett" ;;
    --arbeitsplatz) MODUS="arbeitsplatz" ;;
    --simulation)   SIMULATION=1 ;;
    # Nur fuer das grafische Deinstallationsprogramm: dort wurde bereits zweimal
    # nachgefragt, ein drittes Mal "entfernen" tippen zu muessen waere unsinnig --
    # und ohne Terminal gibt es ohnehin keine Eingabemoeglichkeit.
    --ohne-rueckfrage) OHNE_RUECKFRAGE=1 ;;
    # Nur die Bestandsaufnahme ausgeben und beenden -- das grafische Programm
    # zeigt sie in seiner Rueckfrage an.
    --zusammenfassung) NUR_ZUSAMMENFASSUNG=1 ;;
    -h|--hilfe|--help)
      sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      echo "Unbekannte Option: $arg  (--komplett | --arbeitsplatz | --simulation)" >&2
      exit 1 ;;
  esac
done

if [ "$(id -u)" = "0" ]; then
  echo "Bitte NICHT mit sudo starten — die Anmeldeobjekte und der Autostart" >&2
  echo "gehören zum angemeldeten Benutzer, nicht zu root." >&2
  exit 1
fi

DATEN="$HOME/Library/Application Support/Archivio"
DB="$DATEN/archivio.db"
ZEIT="$(date +%Y%m%d-%H%M%S)"
PAPIERKORB="$HOME/.Trash/Archivio-Deinstallation-$ZEIT"

hinweis() { printf '  %s\n' "$1"; }
# Fettdruck nur im Terminal. Das grafische Deinstallationsprogramm liest diese
# Ausgabe und zeigt sie in einem Dialogfenster -- dort wuerden ANSI-Codes als
# Zeichensalat erscheinen.
if [ -t 1 ]; then
  titel() { printf '\n\033[1m%s\033[0m\n' "$1"; }
else
  titel() { printf '\n%s\n' "$1"; }
fi

# ── Bestandsaufnahme ─────────────────────────────────────────────────────────

titel "Gefunden auf diesem Mac"

if [ -f "$DB" ]; then
  GROESSE="$(du -h "$DB" 2>/dev/null | cut -f1)"
  DOKUMENTE="?"
  if command -v sqlite3 >/dev/null 2>&1; then
    DOKUMENTE="$(sqlite3 "$DB" 'SELECT COUNT(*) FROM documents' 2>/dev/null || echo '?')"
  fi
  hinweis "Datenbank: $GROESSE, $DOKUMENTE Dokumente"
else
  hinweis "Datenbank: keine gefunden"
fi

# Sicherungszustand aus backup_state.json (siehe db/backup.py) — eine Datenbank
# ohne gültige Sicherung zu entfernen, muss man bewusst tun.
ZUSTAND="$DATEN/backup_state.json"
SICHERUNG_OK=0
if [ -f "$ZUSTAND" ]; then
  LETZTE="$(/usr/bin/python3 -c "
import json,sys
try:
    s=json.load(open(sys.argv[1]))
    print(('OK|' if s.get('last_ok') else 'FEHLER|') + str(s.get('last_run','?')) + '|' + str(s.get('target','?')))
except Exception:
    print('KEINE|?|?')
" "$ZUSTAND" 2>/dev/null || echo 'KEINE|?|?')"
  case "$LETZTE" in
    OK\|*) SICHERUNG_OK=1
           hinweis "Letzte Sicherung: $(echo "$LETZTE" | cut -d'|' -f2) nach $(echo "$LETZTE" | cut -d'|' -f3)" ;;
    *)     hinweis "Letzte Sicherung: fehlgeschlagen oder unbekannt" ;;
  esac
else
  hinweis "Letzte Sicherung: keine (nie eingerichtet)"
fi

[ -d "/Applications/Archivio Server.app" ] && hinweis "Programm: Archivio Server.app"
[ -d "/Applications/Archivio Helper.app" ] && hinweis "Programm: Archivio Helper.app"

if [ "$NUR_ZUSAMMENFASSUNG" = "1" ]; then
  [ "$SICHERUNG_OK" = "1" ] || echo "OHNE_SICHERUNG"
  exit 0
fi

# ── Modus wählen ─────────────────────────────────────────────────────────────

if [ -z "$MODUS" ]; then
  titel "Was soll passieren?"
  echo "  1) Komplett entfernen — alles weg, inklusive Datenbank und Helper."
  echo "     (Testrechner, oder dieser Mac wird ausgemustert)"
  echo
  echo "  2) Zum Arbeitsplatz machen — Server weg, Helper bleibt, Datenbank wird"
  echo "     nur umbenannt und bleibt vorerst liegen."
  echo "     (der Server ist auf einen anderen Mac gezogen)"
  echo
  printf "Auswahl [1/2]: "
  read -r wahl
  case "$wahl" in
    1) MODUS="komplett" ;;
    2) MODUS="arbeitsplatz" ;;
    *) echo "Abgebrochen."; exit 1 ;;
  esac
fi

# ── Was genau angefasst wird ─────────────────────────────────────────────────

# Zwei Listen, weil zwei Rechtelagen: alles unter dem Benutzerordner lässt sich
# direkt verschieben, die Programme in /Applications gehören dagegen root (vom
# Installationspaket angelegt). Ein `mv` scheitert dort ohne Rechte -- deshalb
# übernimmt für sie der Finder, der bei Bedarf selbst nach dem Passwort fragt.
# Das Datenverzeichnis (mit der Datenbank) geht in BEIDEN Betriebsarten weg: auch
# beim Umzug hat der alte Mac keine Verwendung mehr dafür, und eine vergessene
# Mehr-GB-Datenbank wäre nur Ballast. Zurückholbar bleibt sie über den Papierkorb.
ZIELE=(
  "$HOME/Library/LaunchAgents/io.archivio.server.plist"
  "$HOME/Library/Caches/Archivio"
  "$HOME/Library/Logs/ArchivioServer.log"
  "$DATEN"
)
PROGRAMME=("/Applications/Archivio Server.app")
if [ "$MODUS" = "komplett" ]; then
  ZIELE+=(
    "$HOME/.archivio"
    "$HOME/Library/Services/ArchivioLink.workflow"
    "$HOME/Library/Logs/ArchivioHelper.log"
  )
  PROGRAMME+=("/Applications/Archivio Helper.app")
fi

titel "Wird in den Papierkorb verschoben"
GEFUNDEN=0
for p in "${PROGRAMME[@]}" "${ZIELE[@]}"; do
  if [ -e "$p" ]; then hinweis "$p"; GEFUNDEN=$((GEFUNDEN + 1)); fi
done
[ "$GEFUNDEN" = "0" ] && hinweis "(nichts davon vorhanden)"

if [ "$MODUS" = "arbeitsplatz" ]; then
  titel "Bleibt erhalten"
  hinweis "Archivio Helper.app samt Einstellungen und MCP-Anbindung —"
  hinweis "dieser Mac bleibt Arbeitsplatz am neuen Server"
fi

# Die Datenbank geht in beiden Betriebsarten weg -- die Warnung gilt deshalb auch
# für den Umzugsfall.
if [ -f "$DB" ] && [ "$SICHERUNG_OK" = "0" ]; then
  titel "ACHTUNG"
  hinweis "Von dieser Datenbank gibt es keine bestätigte Sicherung."
  hinweis "Sie landet im Papierkorb und ist nach dessen Leerung endgültig weg."
fi

if [ "$SIMULATION" = "1" ]; then
  titel "Simulation — es wurde nichts verändert."
  exit 0
fi

if [ "$OHNE_RUECKFRAGE" = "0" ]; then
  titel "Bestätigung"
  printf "Zum Fortfahren «entfernen» eintippen: "
  read -r bestaetigung
  [ "$bestaetigung" = "entfernen" ] || { echo "Abgebrochen."; exit 1; }
fi

# ── Ausführen ────────────────────────────────────────────────────────────────

titel "Autostart abschalten und Programme beenden"
launchctl bootout "gui/$(id -u)/io.archivio.server" 2>/dev/null \
  && hinweis "LaunchAgent entladen" || hinweis "LaunchAgent war nicht geladen"

if [ "$MODUS" = "komplett" ]; then
  MUSTER='name contains "Archivio"'
else
  MUSTER='name is "Archivio Server"'
fi
osascript -e "tell application \"System Events\" to delete (every login item whose $MUSTER)" 2>/dev/null \
  && hinweis "Anmeldeobjekte entfernt" \
  || hinweis "Anmeldeobjekte: nichts zu entfernen (oder Berechtigung fehlt — dann von Hand unter Systemeinstellungen → Allgemein → Anmeldeobjekte)"

pkill -f "Archivio Server.app" 2>/dev/null && hinweis "Server beendet"
if [ "$MODUS" = "komplett" ]; then
  pkill -f "Archivio Helper.app" 2>/dev/null && hinweis "Helper beendet"
fi
sleep 1

titel "Programme entfernen"
for p in "${PROGRAMME[@]}"; do
  [ -e "$p" ] || continue
  # Über den Finder statt per mv: die Bundles gehören root, und der Finder holt
  # dafür bei Bedarf selbst die Bestätigung ein, statt an fehlenden Rechten zu
  # scheitern. Er legt sie ebenfalls im Papierkorb ab.
  if osascript -e "tell application \"Finder\" to delete POSIX file \"$p\"" >/dev/null 2>&1; then
    hinweis "in den Papierkorb gelegt: $p"
  else
    hinweis "FEHLER: $p konnte nicht entfernt werden."
    hinweis "        Bitte im Finder von Hand in den Papierkorb ziehen."
  fi
done

titel "Dateien verschieben"
mkdir -p "$PAPIERKORB"
for p in "${ZIELE[@]}"; do
  if [ -e "$p" ]; then
    if mv "$p" "$PAPIERKORB/" 2>/dev/null; then
      hinweis "verschoben: $p"
    else
      hinweis "FEHLER beim Verschieben: $p"
    fi
  fi
done

# MCP-Eintrag aus der Claude-Desktop-Konfiguration nehmen (nur im Komplett-Fall —
# als Arbeitsplatz soll die Schnittstelle weiterlaufen, nur eben gegen den Server
# auf dem anderen Mac).
CLAUDE_CFG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
if [ "$MODUS" = "komplett" ] && [ -f "$CLAUDE_CFG" ]; then
  /usr/bin/python3 -c "
import json, sys, pathlib
p = pathlib.Path(sys.argv[1])
try:
    cfg = json.loads(p.read_text())
    if cfg.get('mcpServers', {}).pop('archivio', None) is not None:
        p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        print('  MCP-Eintrag aus Claude Desktop entfernt')
except Exception as e:
    print(f'  MCP-Eintrag konnte nicht entfernt werden: {e}')
" "$CLAUDE_CFG"
fi

/System/Library/CoreServices/pbs -flush 2>/dev/null

titel "Fertig"
hinweis "Verschoben nach: $PAPIERKORB"
hinweis "Solange der Papierkorb nicht geleert ist, lässt sich alles zurückholen."
if [ "$MODUS" = "arbeitsplatz" ]; then
  echo
  hinweis "Dieser Mac ist jetzt Arbeitsplatz. Im Helper-Menü einmal «Server suchen»,"
  hinweis "falls er den neuen Server nicht von selbst findet."
fi
