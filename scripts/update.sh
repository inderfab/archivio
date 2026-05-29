#!/bin/bash
ARCHIVIO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ARCHIVIO_DIR"
git pull
.venv/bin/pip install -q -r requirements.txt
launchctl stop io.archivio.server
sleep 2
launchctl start io.archivio.server
echo "Archivio aktualisiert: $(date)"
