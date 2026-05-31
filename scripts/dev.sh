#!/usr/bin/env bash
# Entwicklungsserver: nutzt dieselbe Config/DB wie die Bundle-App.
# Aufruf: bash scripts/dev.sh
set -e

ARCHIVIO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export ARCHIVIO_DATA_DIR="$HOME/Library/Application Support/Archivio"

cd "$ARCHIVIO_DIR"

echo "Config + DB: $ARCHIVIO_DATA_DIR"
echo "Server: http://localhost:8000"
echo ""

exec .venv/bin/uvicorn web.main:app \
  --host 127.0.0.1 \
  --port 8000 \
  --reload \
  --log-level info
