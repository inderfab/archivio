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
        # --entitlements ist hier zwingend, nicht nur kosmetisch: Der Launcher ist ein
        # Bash-Skript, das per `exec` direkt in den eingebetteten python3-Prozess wechselt.
        # Entitlements gelten pro Mach-O-Datei, nicht vererbt ueber exec hinweg — ohne sie
        # hier hat der tatsaechlich laufende python3-Prozess Hardened Runtime OHNE die
        # noetigen Ausnahmen (disable-library-validation, allow-unsigned-executable-memory)
        # und wird vom Kernel beim ersten Versuch, ausfuehrbaren Speicher zu allozieren
        # (numpy/cryptography/lxml/pypdfium2), sofort und ohne jede Fehlermeldung getoetet.
        find "$ROOT" \( -name "*.so" -o -name "*.dylib" -o -perm +111 \) -type f -print0 \
            | while IFS= read -r -d '' f; do
                file "$f" | grep -q 'Mach-O' || continue
                codesign --force --timestamp --options runtime \
                         --entitlements config/entitlements.plist \
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

    # Einreichen und Warten sind bewusst getrennt: `submit --wait` bricht mit einem
    # Fehler ab, wenn die Verbindung zum Notarisierungsdienst waehrend des Wartens
    # abreisst -- und weil der Build mit `set -e` laeuft, war damit ein bereits
    # erfolgreich eingereichtes, von Apple akzeptiertes Paket verloren und der
    # komplette Build musste von vorn. Genau das ist bei v3.3.2 passiert
    # (Submission akzeptiert, Abbruch mit NSURLErrorTimedOut).
    local SUB_ID
    SUB_ID=$(xcrun notarytool submit "$SUBMIT_PATH" \
                 --keychain-profile "$ARCHIVIO_NOTARY_PROFILE" \
                 --no-wait --output-format json \
             | /usr/bin/python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
    if [ -z "$SUB_ID" ]; then
        echo "  ❌ Einreichung fehlgeschlagen (keine Submission-ID)"
        [ -n "$TMP_ZIP" ] && rm -f "$TMP_ZIP"
        return 1
    fi
    echo "  Submission: $SUB_ID"

    local VERSUCH=0
    local STATUS=""
    while [ "$VERSUCH" -lt 5 ]; do
        VERSUCH=$((VERSUCH + 1))
        xcrun notarytool wait "$SUB_ID" \
              --keychain-profile "$ARCHIVIO_NOTARY_PROFILE" --timeout 30m >/dev/null 2>&1 || true
        STATUS=$(xcrun notarytool info "$SUB_ID" \
                     --keychain-profile "$ARCHIVIO_NOTARY_PROFILE" --output-format json 2>/dev/null \
                 | /usr/bin/python3 -c 'import sys,json; print(json.load(sys.stdin).get("status",""))' 2>/dev/null || true)
        case "$STATUS" in
            Accepted) break ;;
            Invalid|Rejected)
                echo "  ❌ Apple hat die Notarisierung abgelehnt (Status: $STATUS)"
                xcrun notarytool log "$SUB_ID" --keychain-profile "$ARCHIVIO_NOTARY_PROFILE" 2>/dev/null | head -40
                [ -n "$TMP_ZIP" ] && rm -f "$TMP_ZIP"
                return 1 ;;
            *)
                echo "  ⚠️  Status noch offen/Verbindung abgerissen (Versuch $VERSUCH/5) — warte 30s"
                sleep 30 ;;
        esac
    done

    [ -n "$TMP_ZIP" ] && rm -f "$TMP_ZIP"

    if [ "$STATUS" != "Accepted" ]; then
        echo "  ❌ Notarisierung nicht bestaetigt (letzter Status: ${STATUS:-unbekannt})"
        return 1
    fi

    xcrun stapler staple "$TARGET"
    echo "  ✓ notarisiert + gestapelt: $TARGET"
}
