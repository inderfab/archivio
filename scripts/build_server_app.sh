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
# Immutable-Flags entfernen (codesign-Bundlesignierung setzt sie) — dann löschen
if [ -d "$APP" ]; then
    xattr -cr "$APP"    2>/dev/null || true
    chflags -R nouchg "$APP" 2>/dev/null || true
    chmod -R u+w "$APP" 2>/dev/null || true
    rm -rf "$APP" 2>/dev/null || { echo "⚠  Konnte $APP nicht löschen — bitte einmal 'sudo rm -rf \"$APP\"' im Terminal ausführen"; exit 1; }
fi

# ── Eingebettetes Python (universal: arm64 + x86_64) ─────────────────────────
# Zum Updaten: nur diese Zeile anpassen (Major.Minor), neu builden — fertig.
PYTHON_VERSION="3.13"

REQ_HASH=$(md5 -q requirements.txt)

_build_python() {
    local PBS_ARCH="$1"   # z.B. aarch64-apple-darwin
    local ARCH_TAG="$2"   # z.B. arm64 oder x86_64

    local PY_BASE="$DIST/.python-base-$ARCH_TAG"
    local PY_INSTALLED="$DIST/.python-installed-$ARCH_TAG"
    local STAMP="$DIST/.python-stamp-$ARCH_TAG"
    local EXPECTED="$PYTHON_VERSION:$PBS_ARCH:$REQ_HASH"

    if [ "$(cat "$STAMP" 2>/dev/null)" = "$EXPECTED" ] && [ -x "$PY_INSTALLED/bin/python3" ]; then
        echo "  $ARCH_TAG: Cache gültig"
        return
    fi

    # Basis-Python herunterladen wenn nötig
    if [ "$(cat "$PY_BASE/.version" 2>/dev/null)" != "$PYTHON_VERSION:$PBS_ARCH" ]; then
        echo "  $ARCH_TAG: Python herunterladen ($PBS_ARCH)…"
        rm -rf "$PY_BASE"
        mkdir -p "$PY_BASE"

        local URL
        URL=$(curl -sLf "https://api.github.com/repos/indygreg/python-build-standalone/releases/latest" \
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

        if [ -z "$URL" ]; then
            echo "  ⚠  $ARCH_TAG: python-build-standalone nicht gefunden"
            return
        fi
        curl -L --progress-bar "$URL" | tar -xz -C "$PY_BASE" --strip-components=1
        echo "$PYTHON_VERSION:$PBS_ARCH" > "$PY_BASE/.version"
    else
        echo "  $ARCH_TAG: Python bereits im Cache"
    fi

    # Pakete installieren
    echo "  $ARCH_TAG: Pakete installieren…"
    rm -rf "$PY_INSTALLED"
    cp -r "$PY_BASE" "$PY_INSTALLED"

    # x86_64-Python auf Apple Silicon via Rosetta ausführen
    local PIP_CMD="$PY_INSTALLED/bin/python3"
    if [ "$ARCH_TAG" = "x86_64" ] && [ "$(uname -m)" = "arm64" ]; then
        PIP_CMD="arch -x86_64 $PY_INSTALLED/bin/python3"
    fi

    $PIP_CMD -m pip install --prefer-binary -q \
        --no-warn-script-location \
        -r requirements.txt \
        rumps requests

    echo "$EXPECTED" > "$STAMP"
    echo "  $ARCH_TAG: Pakete installiert"
}

echo "→ Python-Umgebungen vorbereiten…"
_build_python "aarch64-apple-darwin" "arm64"
_build_python "x86_64-apple-darwin"  "x86_64"

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

# Helper-Version (entkoppelt von der Server-VERSION) — der Server meldet sie ueber
# /api/version, damit der Helper nur bei echten Helper-Aenderungen updaten will.
HELPER_VERSION=$(cat helper/VERSION 2>/dev/null || cat VERSION)
printf '%s' "$HELPER_VERSION" > "$APP/Contents/Resources/HELPER_VERSION"

# Helper-ZIP ins Bundle (für /dashboard/download/helper)
mkdir -p "$APP/Contents/Resources/dist"
cp "dist/archivio-helper-${HELPER_VERSION}.zip" "$APP/Contents/Resources/dist/"

# Python-Umgebungen ins Bundle kopieren und bereinigen
_install_python_to_bundle() {
    local ARCH_TAG="$1"
    local SRC="$DIST/.python-installed-$ARCH_TAG"
    local DST="$APP/Contents/Frameworks/archivio-python-$ARCH_TAG"

    if [ ! -x "$SRC/bin/python3" ]; then
        echo "  ⚠  $ARCH_TAG: kein Python — wird übersprungen"
        return
    fi

    echo "  $ARCH_TAG: kopieren…"
    cp -r "$SRC" "$DST"

    # Unnötige Dateien bereinigen
    find "$DST" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
    find "$DST" -name "*.pyc"  -delete 2>/dev/null || true
    find "$DST" -name "*.dSYM" -type d -exec rm -rf {} + 2>/dev/null || true
    find "$DST" -name "*.pyi"  -delete 2>/dev/null || true
    rm -rf "$DST/lib/python3"*"/site-packages/PyObjCTest" 2>/dev/null || true

    echo "  $ARCH_TAG: $(du -sh "$DST" | cut -f1)"
}

echo "→ Python-Umgebungen ins Bundle kopieren…"
_install_python_to_bundle "arm64"
_install_python_to_bundle "x86_64"

# ── Ad-hoc Code-Signierung ────────────────────────────────────────────────────
# Erforderlich damit macOS Gatekeeper die nativen Bibliotheken (.so, .dylib) zulässt.
if command -v codesign &>/dev/null; then
    echo "→ Ad-hoc Code-Signierung…"
    for ARCH_TAG in arm64 x86_64; do
        PF="$APP/Contents/Frameworks/archivio-python-$ARCH_TAG"
        [ -d "$PF" ] || continue
        # .so und .dylib signieren (inner-to-outer)
        find "$PF" \( -name "*.so" -o -name "*.dylib" \) -type f \
            | while read -r f; do
                codesign -s - --force "$f" 2>/dev/null || true
            done
        # Python-Binary signieren
        find "$PF/bin" -type f \
            | while read -r f; do
                codesign -s - --force "$f" 2>/dev/null || true
            done
    done
    echo "  Signierung abgeschlossen (nur Binaries, nicht Bundle)"
fi

# ── Launcher-Script ────────────────────────────────────────────────────────────
cat > "$APP/Contents/MacOS/Archivio Server" <<'LAUNCHER'
#!/usr/bin/env bash
BUNDLE="$(cd "$(dirname "$0")/.." && pwd)"
RESOURCES="$BUNDLE/Resources"
LOG="$HOME/Library/Logs/ArchivioServer.log"
exec >> "$LOG" 2>&1
echo "$(date): Archivio Server v$(cat "$RESOURCES/VERSION" 2>/dev/null) starting"

# ── 1. Eingebettetes Python (immer bevorzugt — FDA über App-Bundle) ───────────
ARCH=$(uname -m)
EMBEDDED_PY="$BUNDLE/Frameworks/archivio-python-$ARCH/bin/python3"
if [ -x "$EMBEDDED_PY" ]; then
    echo "$(date): Eingebettetes Python ($ARCH): $("$EMBEDDED_PY" --version 2>&1)"
    exec "$EMBEDDED_PY" "$RESOURCES/archivio_server.py"
fi

# ── 2. Fallback: Venv / system Python ────────────────────────────────────────
echo "$(date): Kein eingebettetes Python für $ARCH — Fallback"

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
    echo "$(date): Venv zu alt — wird neu aufgebaut"
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
  echo "⚠️  pkgbuild nicht gefunden – PKG wird übersprungen"
  exit 0
fi

PKG_ROOT=$(mktemp -d)
PKG_SCRIPTS=$(mktemp -d)

mkdir -p "$PKG_ROOT/Applications"
cp -r "$APP" "$PKG_ROOT/Applications/"

cat > "$PKG_SCRIPTS/postinstall" <<'POSTINSTALL'
#!/bin/bash
CURRENT_USER=$(stat -f "%Su" /dev/console 2>/dev/null || echo "")

if [ -n "$CURRENT_USER" ] && [ "$CURRENT_USER" != "root" ]; then
  DATA_DIR="/Users/$CURRENT_USER/Library/Application Support/Archivio"
  sudo -u "$CURRENT_USER" mkdir -p "$DATA_DIR/dist"
  sudo -u "$CURRENT_USER" cp \
    /Applications/Archivio\ Server.app/Contents/Resources/dist/archivio-helper-*.zip \
    "$DATA_DIR/dist/" 2>/dev/null || true
fi

# Quarantine entfernen (verhindert Gatekeeper-Blockierung bei unsigned App)
xattr -cr /Applications/Archivio\ Server.app 2>/dev/null || true

if [ -n "$CURRENT_USER" ] && [ "$CURRENT_USER" != "root" ]; then
  USER_UID=$(id -u "$CURRENT_USER")
  LA_DIR="/Users/$CURRENT_USER/Library/LaunchAgents"
  PLIST="$LA_DIR/io.archivio.server.plist"
  LOG_DIR="/Users/$CURRENT_USER/.archivio/logs"
  sudo -u "$CURRENT_USER" mkdir -p "$LA_DIR" "$LOG_DIR"

  # LaunchAgent: hält die App IMMER am Leben (KeepAlive=true) — auch nach einem
  # sauberen Exit 0 (macOS Logout/Ruhezustand/Update). Frueher (SuccessfulExit=false)
  # blieb der Server nach einem Exit 0 uebers Wochenende tot. ThrottleInterval
  # verhindert eine Absturz-Schleife. Bewusstes Beenden erfolgt ueber
  # 'launchctl bootout' im Quit-Menue.
  cat > "$PLIST" <<'PLISTEOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>io.archivio.server</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Applications/Archivio Server.app/Contents/MacOS/Archivio Server</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>ProcessType</key>
  <string>Interactive</string>
  <key>StandardErrorPath</key>
  <string>LOGDIRPLACEHOLDER/launchd-server.log</string>
  <key>StandardOutPath</key>
  <string>LOGDIRPLACEHOLDER/launchd-server.log</string>
</dict>
</plist>
PLISTEOF
  # Log-Pfad einsetzen (heredoc oben ist quoted, daher Platzhalter ersetzen)
  sed -i '' "s#LOGDIRPLACEHOLDER#$LOG_DIR#g" "$PLIST"
  chown "$CURRENT_USER" "$PLIST"

  # Altes Login-Item entfernen — Autostart läuft jetzt über launchd
  sudo -u "$CURRENT_USER" osascript -e \
    'tell application "System Events" to delete (every login item whose name is "Archivio Server")' 2>/dev/null || true

  # Alten Agent entladen
  sudo -u "$CURRENT_USER" launchctl bootout "gui/$USER_UID/io.archivio.server" 2>/dev/null || true
  # Evtl. manuell/ausserhalb launchd laufende Instanz beenden, damit danach nur
  # die launchd-verwaltete (KeepAlive) laeuft — sonst zwei Menueleisten-Icons.
  pkill -f "Contents/Resources/archivio_server.py" 2>/dev/null || true
  sleep 1
  # Neuen Agent laden (RunAtLoad + KeepAlive starten die App)
  if ! sudo -u "$CURRENT_USER" launchctl bootstrap "gui/$USER_UID" "$PLIST" 2>/dev/null; then
    sudo -u "$CURRENT_USER" launchctl load "$PLIST" 2>/dev/null \
      || sudo -u "$CURRENT_USER" open -a "Archivio Server" 2>/dev/null || true
  fi
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
