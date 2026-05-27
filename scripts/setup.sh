#!/bin/bash
# Einmalig auf dem iMac ausführen: bash scripts/setup.sh
set -e
cd /Users/pas/archivio

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r requirements-menubar.txt

mkdir -p logs

echo "Setup abgeschlossen. Weiter mit:"
echo "  cp scripts/ch.strut.archivio.plist ~/Library/LaunchAgents/"
echo "  cp scripts/ch.strut.archivio-menubar.plist ~/Library/LaunchAgents/"
echo "  launchctl load ~/Library/LaunchAgents/ch.strut.archivio.plist"
echo "  launchctl load ~/Library/LaunchAgents/ch.strut.archivio-menubar.plist"
