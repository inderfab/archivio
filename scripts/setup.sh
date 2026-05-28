#!/bin/bash
# Einmalig auf dem iMac ausführen: bash scripts/setup.sh
set -e
cd /Users/pas/archivio

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

mkdir -p logs

PLIST_SRC="scripts/ch.strut.archivio.plist"
PLIST_DST="$HOME/Library/LaunchAgents/ch.strut.archivio.plist"

cp "$PLIST_SRC" "$PLIST_DST"
launchctl load "$PLIST_DST"

echo ""
echo "✓ Archivio läuft. Browser: http://localhost:8000"
echo ""
echo "Zum Starten/Öffnen: Doppelklick auf 'scripts/Archivio starten.command'"
