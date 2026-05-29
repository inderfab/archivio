#!/bin/bash
# Einmalig ausführen: bash scripts/setup.sh
set -e

ARCHIVIO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ARCHIVIO_DIR"

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

mkdir -p logs

# Datenverzeichnis in ~/Library/Application Support/Archivio
DATA_DIR="$HOME/Library/Application Support/Archivio"
mkdir -p "$DATA_DIR/logs"
if [ ! -f "$DATA_DIR/config.yaml" ] && [ -f "config.yaml.example" ]; then
  cp config.yaml.example "$DATA_DIR/config.yaml"
  echo "config.yaml nach $DATA_DIR kopiert — bitte anpassen."
fi

# LaunchAgent-Plist dynamisch generieren
PLIST_DST="$HOME/Library/LaunchAgents/io.archivio.server.plist"
cat > "$PLIST_DST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>io.archivio.server</string>

  <key>ProgramArguments</key>
  <array>
    <string>$ARCHIVIO_DIR/.venv/bin/uvicorn</string>
    <string>web.main:app</string>
    <string>--host</string>
    <string>0.0.0.0</string>
    <string>--port</string>
    <string>8000</string>
  </array>

  <key>WorkingDirectory</key>
  <string>$ARCHIVIO_DIR</string>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>$DATA_DIR/logs/server.log</string>

  <key>StandardErrorPath</key>
  <string>$DATA_DIR/logs/server.log</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$ARCHIVIO_DIR/.venv/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>ARCHIVIO_DATA_DIR</key>
    <string>$DATA_DIR</string>
  </dict>
</dict>
</plist>
PLIST

launchctl load "$PLIST_DST"

echo ""
echo "✓ Archivio läuft. Browser: http://localhost:8000"
echo "  Konfiguration: $DATA_DIR/config.yaml"
echo ""
