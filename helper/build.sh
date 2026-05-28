#!/usr/bin/env bash
# Baut Archivio Helper.app als macOS Bundle und packt sie als ZIP.
set -e

DIST="dist"
APP_NAME="Archivio Helper"
APP="$DIST/$APP_NAME.app"
VERSION=$(cat VERSION)

mkdir -p "$DIST"

# ── Bundle-Struktur ────────────────────────────────────────────────────────────
mkdir -p "$APP/Contents/MacOS"
mkdir -p "$APP/Contents/Resources"

# Launcher-Script
cat > "$APP/Contents/MacOS/Archivio Helper" <<'LAUNCHER'
#!/usr/bin/env bash
DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
cd "$DIR"

# Abhängigkeiten bei Erststart installieren
VENV="$DIR/.venv"
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet -r "$DIR/requirements.txt"
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

# PkgInfo
echo -n "APPL????" > "$APP/Contents/PkgInfo"

# ── ZIP ────────────────────────────────────────────────────────────────────────
cd "$DIST"
zip -r "archivio-helper-${VERSION}.zip" "$APP_NAME.app"
cd ..

echo "✓ $DIST/archivio-helper-${VERSION}.zip erstellt"
