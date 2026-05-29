#!/usr/bin/env bash
# Baut Archivio Helper.app als macOS Bundle und ZIP.
# Aufruf: bash scripts/build_helper_app.sh
set -e
cd "$(dirname "$0")/.."
bash helper/build.sh
echo "✓ Helper-Build abgeschlossen (dist/)"
