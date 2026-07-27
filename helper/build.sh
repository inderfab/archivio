#!/usr/bin/env bash
# Baut Archivio Helper.app als macOS Bundle und packt sie als ZIP.
# Enthaelt ein eingebettetes Python (rumps + requests + mcp) — kein Xcode/pip beim Nutzer noetig.
# Laeuft immer mit cwd = Repo-Wurzel (direkt aufgerufen oder von build_server_app.sh aus).
set -e
source scripts/sign_lib.sh

DIST="dist"
APP_NAME="Archivio Helper"
APP="$DIST/$APP_NAME.app"
# Eigene Helper-Version (entkoppelt von der Server-VERSION).
VERSION=$(cat helper/VERSION 2>/dev/null || cat VERSION)
PYTHON_VERSION="3.13"

mkdir -p "$DIST"

# ── Bundle-Struktur ────────────────────────────────────────────────────────────
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
mkdir -p "$APP/Contents/Resources"

# ── Minimales eingebettetes Python (rumps + requests + mcp) ─────────────────────
# Basis-Python wird vom Server-Build (.python-base-*) wiederverwendet, sonst geladen.
# mcp ist fuer den eingebetteten Archivio-MCP-Server (archivio_mcp.py), den Claude
# Desktop als stdio-Subprozess startet.
_build_helper_python() {
    local PBS_ARCH="$1"   # z.B. aarch64-apple-darwin
    local ARCH_TAG="$2"   # arm64 | x86_64
    local PY_BASE="$DIST/.python-base-$ARCH_TAG"
    local PY_HELPER="$DIST/.python-helper-$ARCH_TAG"
    local STAMP="$DIST/.python-helper-stamp-$ARCH_TAG"
    local EXPECTED="$PYTHON_VERSION:$PBS_ARCH:rumps+requests+mcp"

    # Basis-Python sicherstellen
    if [ "$(cat "$PY_BASE/.version" 2>/dev/null)" != "$PYTHON_VERSION:$PBS_ARCH" ]; then
        echo "  $ARCH_TAG: Basis-Python herunterladen ($PBS_ARCH)…"
        rm -rf "$PY_BASE"; mkdir -p "$PY_BASE"
        local URL
        URL=$(curl -sLf "https://api.github.com/repos/indygreg/python-build-standalone/releases/latest" \
            | python3 -c "
import sys, json
rel = json.load(sys.stdin); arch = '$PBS_ARCH'; py = '$PYTHON_VERSION'
for a in rel['assets']:
    u = a['browser_download_url']
    if (f'cpython-{py}.' in u and arch in u and 'install_only_stripped' in u
            and 'freethreaded' not in u and u.endswith('.tar.gz')):
        print(u); break
" 2>/dev/null || echo "")
        if [ -z "$URL" ]; then
            echo "  ⚠  $ARCH_TAG: python-build-standalone nicht gefunden — übersprungen"
            return
        fi
        curl -L --progress-bar "$URL" | tar -xz -C "$PY_BASE" --strip-components=1
        echo "$PYTHON_VERSION:$PBS_ARCH" > "$PY_BASE/.version"
    fi

    # rumps + requests + mcp installieren (cachebar)
    if [ "$(cat "$STAMP" 2>/dev/null)" != "$EXPECTED" ] || [ ! -x "$PY_HELPER/bin/python3" ]; then
        echo "  $ARCH_TAG: rumps + requests + mcp installieren…"
        rm -rf "$PY_HELPER"; cp -r "$PY_BASE" "$PY_HELPER"
        local PIP="$PY_HELPER/bin/python3"
        if [ "$ARCH_TAG" = "x86_64" ] && [ "$(uname -m)" = "arm64" ]; then
            PIP="arch -x86_64 $PY_HELPER/bin/python3"
        fi
        $PIP -m pip install --prefer-binary -q --no-warn-script-location rumps requests mcp
        echo "$EXPECTED" > "$STAMP"
    else
        echo "  $ARCH_TAG: Cache gültig"
    fi

    # ins Bundle kopieren + bereinigen. Bewusst Resources/, nicht Frameworks/: codesign
    # behandelt jedes Verzeichnis direkt unter Contents/Frameworks/ als vermeintliches
    # Nested-Framework-Bundle und lehnt es ohne gueltige Framework-Struktur ab ("bundle
    # format unrecognized") — das verhindert jede Signierung des Gesamtbundles. app_path()
    # in shared/menubar_bridge.py bleibt unveraendert: gleiche Verschachtelungstiefe.
    local DST="$APP/Contents/Resources/archivio-python-$ARCH_TAG"
    cp -r "$PY_HELPER" "$DST"
    find "$DST" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
    find "$DST" -name "*.pyc"  -delete 2>/dev/null || true
    find "$DST" -name "*.dSYM" -type d -exec rm -rf {} + 2>/dev/null || true
    echo "  $ARCH_TAG: $(du -sh "$DST" | cut -f1)"
}

echo "→ Helper-Python vorbereiten…"
_build_helper_python "aarch64-apple-darwin" "arm64"
_build_helper_python "x86_64-apple-darwin"  "x86_64"

# Code-Signierung der nativen Bibliotheken (ARCHIVIO_SIGN_APP falls gesetzt, sonst ad-hoc
# wie bisher — siehe scripts/sign_lib.sh)
for ARCH_TAG in arm64 x86_64; do
    sign_inner "$APP/Contents/Resources/archivio-python-$ARCH_TAG"
done

# ── Launcher: eingebettetes Python bevorzugen, venv nur als Fallback ────────────
cat > "$APP/Contents/MacOS/Archivio Helper" <<'LAUNCHER'
#!/usr/bin/env bash
BUNDLE="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$BUNDLE/Resources"
LOG="$HOME/Library/Logs/ArchivioHelper.log"
exec >> "$LOG" 2>&1
echo "$(date): Archivio Helper starting"

# 1. Eingebettetes Python (kein Xcode/pip noetig)
ARCH=$(uname -m)
EMBEDDED_PY="$DIR/archivio-python-$ARCH/bin/python3"
if [ -x "$EMBEDDED_PY" ]; then
    echo "$(date): Eingebettetes Python ($ARCH): $("$EMBEDDED_PY" --version 2>&1)"
    exec "$EMBEDDED_PY" "$DIR/archivio_helper.py"
fi

# 2. Fallback: System-Python + venv (nur wenn kein eingebettetes Python vorhanden)
echo "$(date): Kein eingebettetes Python fuer $ARCH — Fallback venv"
PYTHON=""
for p in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$p" ]; then PYTHON="$p"; break; fi
done
if [ -z "$PYTHON" ]; then
  osascript -e 'display alert "Archivio Helper" message "Python 3 nicht gefunden." as critical'
  echo "$(date): ERROR - python3 not found"
  exit 1
fi
VENV="$DIR/.venv"
if [ ! -d "$VENV" ]; then
  osascript -e 'display notification "Erstinstallation läuft, bitte warten…" with title "Archivio Helper"'
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade pip
  "$VENV/bin/pip" install rumps requests mcp
fi
exec "$VENV/bin/python3" "$DIR/archivio_helper.py"
LAUNCHER
chmod +x "$APP/Contents/MacOS/Archivio Helper"

# Python-Script und Ressourcen
cp helper/archivio_helper.py    "$APP/Contents/Resources/"
cp helper/archivio_mcp.py       "$APP/Contents/Resources/"
cp shared/menubar_bridge.py     "$APP/Contents/Resources/"
cp helper/config.json         "$APP/Contents/Resources/"
cp helper/requirements.txt    "$APP/Contents/Resources/"
cp helper/icon.png            "$APP/Contents/Resources/"
cp -r helper/ArchivioLink.workflow "$APP/Contents/Resources/"
cp archivio.icns              "$APP/Contents/Resources/"
printf '%s' "$VERSION"      > "$APP/Contents/Resources/VERSION"

# Info.plist
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key>
  <string>io.archivio.helper</string>
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
  <key>CFBundleIconFile</key>
  <string>archivio</string>
  <key>NSAppleEventsUsageDescription</key>
  <string>Archivio Helper muss Dateien öffnen können.</string>
</dict>
</plist>
PLIST

echo -n "APPL????" > "$APP/Contents/PkgInfo"

# Bundle signieren + notarisieren (vor dem ZIP — Staple in ein fertiges Zip
# funktioniert nicht, siehe scripts/sign_lib.sh). --deep bewusst nicht mehr verwendet
# (von Apple deprecated, war nie der richtige Weg) — sign_bundle signiert von innen nach
# aussen selbst.
sign_bundle "$APP"
notarize_and_staple "$APP"

# ── ZIP ────────────────────────────────────────────────────────────────────────
cd "$DIST"
zip -qr "archivio-helper-${VERSION}.zip" "$APP_NAME.app" \
    -x "**/__pycache__/*" -x "**/*.pyc"
cd ..

echo "✓ $DIST/archivio-helper-${VERSION}.zip erstellt"
