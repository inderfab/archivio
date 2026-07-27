#!/usr/bin/env bash
# Gemeinsame Signier-/Notarisierungs-Funktionen für scripts/build_server_app.sh und
# helper/build.sh — vermeidet doppelten Code zwischen den beiden Build-Pfaden (analog zu
# shared/menubar_bridge.py für den Python-Code der beiden Apps).
#
# Erwartet cwd = Repo-Wurzel (beide aufrufenden Skripte laufen so).
#
# Drei optionale Umgebungsvariablen steuern echtes Signieren/Notarisieren:
#   ARCHIVIO_SIGN_APP        z.B. "Developer ID Application: Firma GmbH (TEAMID)"
#   ARCHIVIO_SIGN_INSTALLER  z.B. "Developer ID Installer: Firma GmbH (TEAMID)"
#   ARCHIVIO_NOTARY_PROFILE  Name des per `xcrun notarytool store-credentials` gespeicherten
#                            Keychain-Profils, z.B. "archivio-notary"
#
# Fehlen sie, bauen beide Skripte unveraendert ad-hoc-signiert weiter (lokale Entwicklung
# ohne Zertifikat funktioniert damit exakt wie bisher) — es wird pro Build-Lauf einmal
# gewarnt, kein Fehler.

_ARCHIVIO_ADHOC_WARNED=""

_warn_adhoc_once() {
    if [ -z "$_ARCHIVIO_ADHOC_WARNED" ]; then
        echo "⚠️  ARCHIVIO_SIGN_APP nicht gesetzt — baue ad-hoc-signiert, nicht notarisierbar."
        _ARCHIVIO_ADHOC_WARNED=1
    fi
}

# sign_inner <bundle-oder-frameworks-pfad>
# Signiert alle Mach-O-Dateien (.so, .dylib, ausfuehrbare Dateien) rekursiv darunter,
# von innen nach aussen wie von Apple gefordert.
sign_inner() {
    local ROOT="$1"
    [ -d "$ROOT" ] || return 0
    if [ -n "$ARCHIVIO_SIGN_APP" ]; then
        find "$ROOT" \( -name "*.so" -o -name "*.dylib" -o -perm +111 \) -type f -print0 \
            | while IFS= read -r -d '' f; do
                file "$f" | grep -q 'Mach-O' || continue
                codesign --force --timestamp --options runtime \
                         --sign "$ARCHIVIO_SIGN_APP" "$f" 2>/dev/null || true
            done
    else
        _warn_adhoc_once
        find "$ROOT" \( -name "*.so" -o -name "*.dylib" \) -type f | while read -r f; do
            codesign -s - --force "$f" 2>/dev/null || true
        done
        find "$ROOT/bin" -type f 2>/dev/null | while read -r f; do
            codesign -s - --force "$f" 2>/dev/null || true
        done
    fi
}

# sign_bundle <app-pfad>
# Signiert zuerst alles innerhalb (sign_inner), dann das Bundle als Ganzes. Mit Zertifikat
# inkl. Entitlements + Hardened Runtime; ohne Zertifikat ad-hoc ohne Entitlements (die
# ergeben ohne echte Signatur keinen Sinn). Bricht bei fehlgeschlagener Verifikation ab.
sign_bundle() {
    local APP_PATH="$1"
    sign_inner "$APP_PATH"
    if [ -n "$ARCHIVIO_SIGN_APP" ]; then
        codesign --force --timestamp --options runtime \
                 --entitlements config/entitlements.plist \
                 --sign "$ARCHIVIO_SIGN_APP" "$APP_PATH"
        codesign --verify --deep --strict --verbose=2 "$APP_PATH"
        echo "  ✓ signiert: $APP_PATH"
    else
        _warn_adhoc_once
        codesign -s - --force "$APP_PATH" 2>/dev/null || true
    fi
}

# notarize_and_staple <app-oder-pkg-pfad>
# No-op mit Warnung falls ARCHIVIO_NOTARY_PROFILE fehlt. Bei .app: fuer die Einreichung in
# ein temporaeres Zip verpackt (ditto), gestapelt wird aber die .app selbst — ein Staple in
# das Einreichungs-Zip funktioniert nicht.
notarize_and_staple() {
    local TARGET="$1"
    if [ -z "$ARCHIVIO_NOTARY_PROFILE" ]; then
        echo "⚠️  ARCHIVIO_NOTARY_PROFILE nicht gesetzt — überspringe Notarisierung für $TARGET"
        return 0
    fi
    if [ -z "$ARCHIVIO_SIGN_APP" ]; then
        echo "⚠️  Notarisierung übersprungen — $TARGET ist nicht signiert (ARCHIVIO_SIGN_APP fehlt)"
        return 0
    fi

    echo "→ Notarisiere $TARGET (kann mehrere Minuten dauern)…"
    local SUBMIT_PATH="$TARGET"
    local TMP_ZIP=""
    case "$TARGET" in
        *.app)
            TMP_ZIP=$(mktemp -t archivio-notarize).zip
            ditto -c -k --keepParent "$TARGET" "$TMP_ZIP"
            SUBMIT_PATH="$TMP_ZIP"
            ;;
    esac

    xcrun notarytool submit "$SUBMIT_PATH" --keychain-profile "$ARCHIVIO_NOTARY_PROFILE" --wait

    [ -n "$TMP_ZIP" ] && rm -f "$TMP_ZIP"

    xcrun stapler staple "$TARGET"
    echo "  ✓ notarisiert + gestapelt: $TARGET"
}
