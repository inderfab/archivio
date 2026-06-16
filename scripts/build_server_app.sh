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

# ── Eingebettetes Python ──────────────────────────────────────────────────────
# Zum Updaten: nur diese Zeile anpassen (Major.Minor), neu builden — fertig.
PYTHON_VERSION="3.13"

ARCH=$(uname -m)
[ "$ARCH" = "arm64" ] && PBS_ARCH="aarch64-apple-darwin" || PBS_ARCH="x86_64-apple-darwin"

PY_BASE="$DIST/.python-base"           # Download-Cache (nur Python, keine Pakete)
PY_INSTALLED="$DIST/.python-installed" # Build-Cache (Python + alle Pakete)
REQ_HASH=$(md5 -q requirements.txt)
STAMP_FILE="$DIST/.python-stamp"
EXPECTED_STAMP="$PYTHON_VERSION:$PBS_ARCH:$REQ_HASH"

if [ "$(cat "$STAMP_FILE" 2>/dev/null)" != "$EXPECTED_STAMP" ]; then

    # Basis-Python: nur herunterladen wenn Version/Architektur geändert
    CACHED_PY="$(cat "$PY_BASE/.version" 2>/dev/null || echo "")"
    if [ "$CACHED_PY" != "$PYTHON_VERSION:$PBS_ARCH" ]; then
        echo "→ python-build-standalone $PYTHON_VERSION ($PBS_ARCH) herunterladen…"
        rm -rf "$PY_BASE"
        mkdir -p "$PY_BASE"

        PBS_URL=$(curl -sLf "https://api.github.com/repos/indygreg/python-build-standalone/releases/latest" \
            | python3 -c "
import sys, json
rel = json.load(sys.stdin)
arch = '$PBS_ARCH'
py  = '$PYTHON_VERSION'
for a in rel['assets']:
    u = a['browser_download_url']
    if (f'cpython-{py}.' in u and arch in u
            and 'install_only_stripped' in u
            and 'freethreaded' not in u
            and u.endswith('.tar.gz')):
        print(u); break
" 2>/dev/null || echo "")

        if [ -z "$PBS_URL" ]; then
            echo "⚠  python-build-standalone $PYTHON_VERSION nicht gefunden – eingebettetes Python wird übersprungen"
        else
            curl -L --progress-bar "$PBS_URL" | tar -xz -C "$PY_BASE" --strip-components=1
            echo "$PYTHON_VERSION:$PBS_ARCH" > "$PY_BASE/.version"
            echo "  Python $PYTHON_VERSION bereit"
        fi
    else
        echo "→ Python $PYTHON_VERSION bereits im Cache"
    fi

    # Pakete installieren (nur wenn Python vorhanden)
    if [ -x "$PY_BASE/bin/python3" ]; then
        echo "→ Python-Pakete installieren (dauert ~2 Minuten beim ersten Mal)…"
        rm -rf "$PY_INSTALLED"
        cp -r "$PY_BASE" "$PY_INSTALLED"
        "$PY_INSTALLED/bin/python3" -m pip install --prefer-binary -q \
            --no-warn-script-location \
            -r requirements.txt \
            rumps requests
        echo "$EXPECTED_STAMP" > "$STAMP_FILE"
        echo "  Pakete installiert"
    fi
else
    echo "→ Python-Umgebung unverändert (Cache gültig)"
fi

# ── Bundle-Struktur ────────────────────────────────────────────────────────────
mkdir -p "$APP/Contents/MacOS"
mkdir -p "$APP/Contents/Resources"
mkdir -p "$APP/Contents/Frameworks"

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

# Eingebettetes Python ins Bundle kopieren
if [ -d "$PY_INSTALLED" ] && [ -x "$PY_INSTALLED/bin/python3" ]; then
    echo "→ Eingebettetes Python ins Bundle kopieren…"
    cp -r "$PY_INSTALLED" "$APP/Contents/Frameworks/archivio-python"
    # Unnötige Dateien bereinigen
    PF="$APP/Contents/Frameworks/archivio-python"
    find "$PF" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
    find "$PF" -name "*.pyc"  -delete 2>/dev/null || true
    find "$PF" -name "*.dSYM" -type d -exec rm -rf {} + 2>/dev/null || true
    find "$PF" -name "*.pyi"  -delete 2>/dev/null || true
    # PyObjCTest: Testcode von rumps, nicht benötigt
    rm -rf "$PF/lib/python3"*"/site-packages/PyObjCTest" 2>/dev/null || true
    echo "  $(du -sh "$PF" | cut -f1) eingebettet"
else
    echo "⚠  Kein eingebettetes Python — Fallback auf system Python"
fi

# ── Launcher-Script ────────────────────────────────────────────────────────────
cat > "$APP/Contents/MacOS/Archivio Server" <<'LAUNCHER'
#!/usr/bin/env bash
BUNDLE="$(cd "$(dirname "$0")/.." && pwd)"
RESOURCES="$BUNDLE/Resources"
LOG="$HOME/Library/Logs/ArchivioServer.log"
exec >> "$LOG" 2>&1
echo "$(date): Archivio Server v$(cat "$RESOURCES/VERSION" 2>/dev/null) starting"

# ── 1. Eingebettetes Python (immer bevorzugt) ─────────────────────────────────
# Hat Full Disk Access über das App-Bundle — keine separate Freigabe nötig.
EMBEDDED_PY="$BUNDLE/Frameworks/archivio-python/bin/python3"
if [ -x "$EMBEDDED_PY" ]; then
    echo "$(date): Eingebettetes Python: $("$EMBEDDED_PY" --version 2>&1)"
    exec "$EMBEDDED_PY" "$RESOURCES/archivio_server.py"
fi

# ── 2. Fallback: Venv aus früherer Installation (oder Erstinstallation) ───────
echo "$(date): Kein eingebettetes Python — Fallback auf system Python"

# python.org-Framework zuerst prüfen (hat FDA via Python Launcher.app)
PYTHON=""
for p in \
  /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
  /usr/local/bin/python3 \
  /opt/homebrew/bin/python3 \
  /usr/bin/python3; do
  if [ -x "$p" ]; then
    PYMINOR=$("$p" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo "0")
    if [ "$PYMINOR" -ge 11 ]; then
      PYTHON="$p"
      break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  osascript -e 'display alert "Archivio Server" message "Python 3.11 (oder neuer) wird benötigt.\n\nBitte installieren:\npython.org/downloads" as critical'
  exit 1
fi
echo "$(date): System-Python: $PYTHON ($("$PYTHON" --version 2>&1))"

DATA_DIR="$HOME/Library/Application Support/Archivio"
mkdir -p "$DATA_DIR/logs"
VENV="$DATA_DIR/.venv"
CURRENT_VERSION=$(cat "$RESOURCES/VERSION" 2>/dev/null || echo "unknown")
VERSION_STAMP="$DATA_DIR/.venv_version"

if [ -d "$VENV" ]; then
  VENV_MINOR=$("$VENV/bin/python3" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo "0")
  if [ "$VENV_MINOR" -lt 11 ]; then
    echo "$(date): Venv Python 3.$VENV_MINOR zu alt — wird neu aufgebaut"
    rm -rf "$VENV"
  fi
fi

if [ ! -d "$VENV" ]; then
  osascript -e 'display notification "Erstinstallation läuft, bitte warten…" with title "Archivio Server"'
  echo "$(date): Erstinstallation – venv wird erstellt"
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade pip -q
  "$VENV/bin/pip" install --prefer-binary -r "$RESOURCES/requirements.txt" -q
  "$VENV/bin/pip" install --prefer-binary rumps requests -q
  echo "$CURRENT_VERSION" > "$VERSION_STAMP"
  echo "$(date): Installation abgeschlossen"
elif [ "$(cat $VERSION_STAMP 2>/dev/null)" != "$CURRENT_VERSION" ]; then
  echo "$(date): Neue Version $CURRENT_VERSION – Abhängigkeiten aktualisieren"
  "$VENV/bin/pip" install -q --prefer-binary -r "$RESOURCES/requirements.txt" >/dev/null 2>&1 || true
  "$VENV/bin/pip" install -q --prefer-binary rumps requests >/dev/null 2>&1 || true
  echo "$CURRENT_VERSION" > "$VERSION_STAMP"
fi

exec "$VENV/bin/python3" "$RESOURCES/archivio_server.py"
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
zip -r "archivio-server-${VERSION}.zip" "$APP_NAME.app" \
    -x "**/__pycache__/*" -x "**/*.pyc"
cd ..
echo "✓ $DIST/archivio-server-${VERSION}.zip erstellt"

# ── PKG-Installer ─────────────────────────────────────────────────────────────
if ! command -v pkgbuild &>/dev/null; then
  echo "⚠️  pkgbuild nicht gefunden – PKG wird übersprungen (Xcode Command Line Tools nötig)"
  exit 0
fi

PKG_ROOT=$(mktemp -d)
PKG_SCRIPTS=$(mktemp -d)

mkdir -p "$PKG_ROOT/Applications"
cp -r "$APP" "$PKG_ROOT/Applications/"

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
