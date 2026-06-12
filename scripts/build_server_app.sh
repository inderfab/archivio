#!/usr/bin/env bash
# Baut Archivio Server.app als macOS Bundle, ZIP und PKG-Installer.
# Aufruf: bash scripts/build_server_app.sh
set -e
cd "$(dirname "$0")/.."

DIST="dist"
APP_NAME="Archivio Server"
APP="$DIST/$APP_NAME.app"
VERSION=$(cat VERSION)
PKG="$DIST/archivio-server-${VERSION}.pkg"

mkdir -p "$DIST"
rm -rf "$APP"

# ── Bundle-Struktur ────────────────────────────────────────────────────────────
mkdir -p "$APP/Contents/MacOS"
mkdir -p "$APP/Contents/Resources"

# Helper zuerst bauen (wird als Download angeboten)
bash helper/build.sh

# Server-Code ins Bundle kopieren
for dir in web scanner db config; do
  cp -r "$dir" "$APP/Contents/Resources/"
done
cp requirements.txt        "$APP/Contents/Resources/"
cp config.yaml.example     "$APP/Contents/Resources/"
cp VERSION                 "$APP/Contents/Resources/"
cp menubar/server_app.py   "$APP/Contents/Resources/archivio_server.py"
cp menubar/icon.png        "$APP/Contents/Resources/"
cp archivio.icns           "$APP/Contents/Resources/"

# Helper-ZIP ins Bundle (für /dashboard/download/helper)
mkdir -p "$APP/Contents/Resources/dist"
cp "dist/archivio-helper-${VERSION}.zip" "$APP/Contents/Resources/dist/"

# ── Launcher-Script ────────────────────────────────────────────────────────────
cat > "$APP/Contents/MacOS/Archivio Server" <<'LAUNCHER'
#!/usr/bin/env bash
DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
LOG="$HOME/Library/Logs/ArchivioServer.log"
exec >> "$LOG" 2>&1
echo "$(date): Archivio Server starting"

# python.org-Framework zuerst prüfen (hat FDA via Python Launcher.app)
# Homebrew-Python absichtlich nachrangig — es hat kein FDA
PYTHON=""
for p in \
  /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
  /usr/local/bin/python3 \
  /opt/homebrew/bin/python3 \
  /usr/bin/python3; do
  if [ -x "$p" ]; then PYTHON="$p"; break; fi
done

if [ -z "$PYTHON" ]; then
  osascript -e 'display alert "Archivio Server" message "Python 3.13 nicht gefunden. Bitte Python von python.org installieren: python.org/downloads" as critical'
  exit 1
fi
echo "$(date): Python: $PYTHON"

DATA_DIR="$HOME/Library/Application Support/Archivio"
mkdir -p "$DATA_DIR/logs"
VENV="$DATA_DIR/.venv"
CURRENT_VERSION=$(cat "$DIR/VERSION" 2>/dev/null || echo "unknown")
VERSION_STAMP="$DATA_DIR/.venv_version"

if [ ! -d "$VENV" ]; then
  osascript -e 'display notification "Erstinstallation läuft, bitte warten…" with title "Archivio Server"'
  echo "$(date): Erstinstallation – venv wird erstellt"
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade pip -q
  "$VENV/bin/pip" install -r "$DIR/requirements.txt" -q
  "$VENV/bin/pip" install rumps requests -q
  echo "$CURRENT_VERSION" > "$VERSION_STAMP"
  echo "$(date): Installation abgeschlossen"
elif [ "$(cat $VERSION_STAMP 2>/dev/null)" != "$CURRENT_VERSION" ]; then
  echo "$(date): Neue Version $CURRENT_VERSION – Abhängigkeiten aktualisieren"
  "$VENV/bin/pip" install -q -r "$DIR/requirements.txt" >/dev/null 2>&1 || true
  "$VENV/bin/pip" install -q rumps requests >/dev/null 2>&1 || true
  echo "$CURRENT_VERSION" > "$VERSION_STAMP"
  echo "$(date): Abhängigkeiten aktualisiert"
fi

exec "$VENV/bin/python3" "$DIR/archivio_server.py"
LAUNCHER
chmod +x "$APP/Contents/MacOS/Archivio Server"

# ── Info.plist ─────────────────────────────────────────────────────────────────
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key>
  <string>io.archivio.server</string>
  <key>CFBundleName</key>
  <string>Archivio Server</string>
  <key>CFBundleDisplayName</key>
  <string>Archivio Server</string>
  <key>CFBundleExecutable</key>
  <string>Archivio Server</string>
  <key>CFBundleVersion</key>
  <string>${VERSION}</string>
  <key>CFBundleShortVersionString</key>
  <string>${VERSION}</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleIconFile</key>
  <string>archivio</string>
  <key>LSUIElement</key>
  <true/>
  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
PLIST

echo -n "APPL????" > "$APP/Contents/PkgInfo"

# ── ZIP (für Self-Update) ─────────────────────────────────────────────────────
cd "$DIST"
zip -r "archivio-server-${VERSION}.zip" "$APP_NAME.app" -x "**/__pycache__/*" -x "**/*.pyc"
cd ..
echo "✓ $DIST/archivio-server-${VERSION}.zip erstellt"

# ── PKG-Installer ─────────────────────────────────────────────────────────────
if ! command -v pkgbuild &>/dev/null; then
  echo "⚠️  pkgbuild nicht gefunden – PKG wird übersprungen (Xcode Command Line Tools nötig)"
  exit 0
fi

PKG_ROOT=$(mktemp -d)
PKG_SCRIPTS=$(mktemp -d)

# App nach /Applications/ installieren
mkdir -p "$PKG_ROOT/Applications"
cp -r "$APP" "$PKG_ROOT/Applications/"

# Postinstall: Autostart + App öffnen
cat > "$PKG_SCRIPTS/postinstall" <<'POSTINSTALL'
#!/bin/bash
CURRENT_USER=$(stat -f "%Su" /dev/console 2>/dev/null || echo "")

# Helper-ZIP in DATA_DIR kopieren damit der Download-Endpoint ihn sicher findet
if [ -n "$CURRENT_USER" ] && [ "$CURRENT_USER" != "root" ]; then
  DATA_DIR="/Users/$CURRENT_USER/Library/Application Support/Archivio"
  sudo -u "$CURRENT_USER" mkdir -p "$DATA_DIR/dist"
  sudo -u "$CURRENT_USER" cp \
    /Applications/Archivio\ Server.app/Contents/Resources/dist/archivio-helper-*.zip \
    "$DATA_DIR/dist/" 2>/dev/null || true
fi

# Login-Item hinzufügen und App starten
if [ -n "$CURRENT_USER" ] && [ "$CURRENT_USER" != "root" ]; then
  sudo -u "$CURRENT_USER" osascript -e \
    'tell application "System Events" to make new login item at end with properties {path:"/Applications/Archivio Server.app", hidden:true}' || true
  sudo -u "$CURRENT_USER" open -a "Archivio Server" || true
fi
exit 0
POSTINSTALL
chmod +x "$PKG_SCRIPTS/postinstall"

pkgbuild \
  --root "$PKG_ROOT" \
  --scripts "$PKG_SCRIPTS" \
  --identifier "io.archivio.server" \
  --version "$VERSION" \
  --install-location "/" \
  "$PKG"

rm -rf "$PKG_ROOT" "$PKG_SCRIPTS"
echo "✓ $PKG erstellt"
