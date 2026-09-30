#!/usr/bin/env bash
# Pull the VPS's daily Parquet export to this machine: the off-site copy of
# Worldwatch's permanent record. Nothing is ever deleted here: a file gone from
# the VPS stays. The snapshots the VPS rewrites each day keep their previous
# versions for 14 days under .replaced/, in case a bad day replaces a good one.
#
#   WW_VPS=peter@categor.io WW_ARCHIVE_DIR=~/worldwatch-archive pull-archive.sh
set -euo pipefail

VPS="${WW_VPS:-peter@categor.io}"
SRC="${WW_EXPORT_SRC:-/var/lib/worldwatch/export/}"
DEST="${WW_ARCHIVE_DIR:-$HOME/worldwatch-archive}"

mkdir -p "$DEST"
rsync -a --partial --exclude='*.tmp' \
  --backup --backup-dir="$DEST/.replaced/$(date -u +%F)" \
  -e "ssh -o BatchMode=yes" "$VPS:$SRC" "$DEST/"
if [ -d "$DEST/.replaced" ]; then
  find "$DEST/.replaced" -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf -- {} +
fi
echo "pulled to $DEST ($(du -sh "$DEST" | cut -f1))"
