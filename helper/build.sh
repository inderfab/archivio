#!/usr/bin/env bash
# Baut Archivio Helper.app als macOS Bundle und packt sie als ZIP.
set -e

DIST="dist"
APP_NAME="Archivio Helper"
APP="$DIST/$APP_NAME.app"
VERSION=$(cat VERSION)

mkdir -p "$DIST"

# ── Bundle-Struktur ────────────────────────────────────────────────────────────
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
mkdir -p "$APP/Contents/Resources"

# Launcher-Script (wird beim Öffnen der App ausgeführt)
cat > "$APP/Contents/MacOS/Archivio Helper" <<'LAUNCHER'
#!/usr/bin/env bash
DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
LOG="$HOME/Library/Logs/ArchivioHelper.log"
exec >> "$LOG" 2>&1
echo "$(date): Archivio Helper starting"

# Python suchen
PYTHON=""
for p in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$p" ]; then PYTHON="$p"; break; fi
done

if [ -z "$PYTHON" ]; then
  osascript -e 'display alert "Archivio Helper" message "Python 3 nicht gefunden. Bitte Python 3 installieren (z.\,B. via Homebrew: brew install python)." as critical'
  echo "$(date): ERROR - python3 not found"
  exit 1
fi
echo "$(date): Using $PYTHON"

# Venv + Abhängigkeiten beim ersten Start installieren
VENV="$DIR/.venv"
if [ ! -d "$VENV" ]; then
  osascript -e 'display notification "Erstinstallation läuft, bitte warten…" with title "Archivio Helper"'
  echo "$(date): Creating venv"
  "$PYTHON" -m venv "$VENV"
  echo "$(date): Installing dependencies"
  "$VENV/bin/pip" install --upgrade pip
  "$VENV/bin/pip" install rumps requests
  echo "$(date): Installation complete"
fi

exec "$VENV/bin/python3" "$DIR/archivio_helper.py"
LAUNCHER
chmod +x "$APP/Contents/MacOS/Archivio Helper"

# Python-Script und Ressourcen
cp helper/archivio_helper.py  "$APP/Contents/Resources/"
cp helper/config.json         "$APP/Contents/Resources/"
cp helper/requirements.txt    "$APP/Contents/Resources/"
cp VERSION                    "$APP/Contents/Resources/"

# Info.plist
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key>
  <string>ch.strut.archivio.helper</string>
  <key>CFBundleName</key>
  <string>Archivio Helper</string>
  <key>CFBundleDisplayName</key>
  <string>Archivio Helper</string>
  <key>CFBundleExecutable</key>
  <string>Archivio Helper</string>
  <key>CFBundleVersion</key>
  <string>${VERSION}</string>
  <key>CFBundleShortVersionString</key>
  <string>${VERSION}</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>LSUIElement</key>
  <true/>
  <key>CFBundleURLTypes</key>
  <array>
    <dict>
      <key>CFBundleURLName</key>
      <string>Archivio File Opener</string>
      <key>CFBundleURLSchemes</key>
      <array>
        <string>archivio</string>
      </array>
    </dict>
  </array>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSAppleEventsUsageDescription</key>
  <string>Archivio Helper muss Dateien öffnen können.</string>
</dict>
</plist>
PLIST

echo -n "APPL????" > "$APP/Contents/PkgInfo"

# ── ZIP ────────────────────────────────────────────────────────────────────────
cd "$DIST"
zip -r "archivio-helper-${VERSION}.zip" "$APP_NAME.app"
cd ..

echo "✓ $DIST/archivio-helper-${VERSION}.zip erstellt"
