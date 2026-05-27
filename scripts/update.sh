#!/bin/bash
cd /Users/pas/archivio
git pull
launchctl stop ch.strut.archivio
sleep 2
launchctl start ch.strut.archivio
echo "Archivio aktualisiert: $(date)"
