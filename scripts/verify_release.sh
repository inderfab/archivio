#!/usr/bin/env bash
# Prüft Signierung + Notarisierung eines gebauten Artefakts.
# Aufruf: scripts/verify_release.sh dist/archivio-server-3.0.17.pkg
#         scripts/verify_release.sh "dist/Archivio Helper.app"
#
# Exit-Code 0 = alles grün, sonst > 0 (erste fehlgeschlagene Prüfung wird gemeldet).
set -e

TARGET="$1"
if [ -z "$TARGET" ] || [ ! -e "$TARGET" ]; then
    echo "Nutzung: $0 <pfad-zu-.pkg-oder-.app>"
    exit 2
fi

FAILED=0

check() {
    local DESC="$1"; shift
    echo "→ $DESC"
    if "$@"; then
        echo "  ✓ OK"
    else
        echo "  ✗ FEHLGESCHLAGEN"
        FAILED=1
    fi
}

case "$TARGET" in
    *.pkg)
        check "Gatekeeper-Installer-Check (spctl)" \
            spctl -a -vvv -t install "$TARGET"
        check "Paket-Signatur (pkgutil)" \
            pkgutil --check-signature "$TARGET"
        check "Notarisierungs-Ticket (stapler)" \
            xcrun stapler validate "$TARGET"
        ;;
    *.app)
        check "Gatekeeper-Ausführungs-Check (spctl)" \
            spctl -a -vvv -t exec "$TARGET"
        check "Bundle-Signatur (codesign)" \
            codesign --verify --deep --strict --verbose=2 "$TARGET"
        check "Notarisierungs-Ticket (stapler)" \
            xcrun stapler validate "$TARGET"
        ;;
    *)
        echo "Unbekannter Artefakt-Typ (erwartet .pkg oder .app): $TARGET"
        exit 2
        ;;
esac

echo
if [ "$FAILED" -eq 0 ]; then
    echo "✓ Alle Prüfungen grün: $TARGET"
else
    echo "✗ Mindestens eine Prüfung fehlgeschlagen: $TARGET"
fi
exit "$FAILED"
