#!/bin/bash
# Doppelklick: startet Archivio falls nicht aktiv, öffnet Browser.
LABEL="ch.strut.archivio"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
URL="http://localhost:8000"

# LaunchAgent installieren falls noch nicht vorhanden
if [ ! -f "$PLIST" ]; then
  SRC="$(dirname "$0")/$LABEL.plist"
  if [ -f "$SRC" ]; then
    cp "$SRC" "$PLIST"
    echo "LaunchAgent installiert."
  fi
fi

# Starten falls nicht aktiv
if ! launchctl list 2>/dev/null | grep -q "$LABEL"; then
  launchctl load "$PLIST" 2>/dev/null
  echo "Archivio wird gestartet…"
  sleep 4
fi

# Browser öffnen
open "$URL"
