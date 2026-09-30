#!/usr/bin/env bash
# Consistent SQLite backup -> restic, for when a cloud copy is added (none is
# set up yet: the daily Parquet export pulled to the local machine, ops/local/,
# is the backup for now). Takes a WAL-safe snapshot via sqlite3 .backup, then
# archives it. Schedule from cron or a systemd timer (daily is plenty).
#
#   RESTIC_REPOSITORY=... RESTIC_PASSWORD_FILE=... ops/backup/restic-backup.sh
set -euo pipefail

WW_DB_PATH="${WW_DB_PATH:-/var/lib/worldwatch/worldwatch.db}"
# A fixed path: restic groups snapshots by host and path, so a fresh temp name
# each run would make every snapshot its own group, and `forget` would keep all.
SNAPSHOT_DIR="${WW_BACKUP_DIR:-/var/cache/worldwatch-backup}"
SNAPSHOT="$SNAPSHOT_DIR/worldwatch.db"
mkdir -p -m 700 "$SNAPSHOT_DIR"
trap 'rm -f "$SNAPSHOT"' EXIT

# .backup is consistent even while the pollers hold the WAL open.
sqlite3 "$WW_DB_PATH" ".backup '$SNAPSHOT'"

restic backup "$SNAPSHOT" --tag worldwatch --host "$(hostname)"
restic forget --tag worldwatch --group-by host,tags --keep-daily 7 --keep-weekly 8 --prune
