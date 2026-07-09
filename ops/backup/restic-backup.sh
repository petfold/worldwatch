#!/usr/bin/env bash
# Consistent SQLite backup -> restic (plugs into the operator's existing
# restic/B2 setup). Takes a WAL-safe snapshot via sqlite3 .backup, then archives
# it. Schedule from cron or a systemd timer (e.g. hourly).
#
#   RESTIC_REPOSITORY=... RESTIC_PASSWORD_FILE=... ops/backup/restic-backup.sh
set -euo pipefail

WW_DB_PATH="${WW_DB_PATH:-/var/lib/worldwatch/worldwatch.db}"
SNAPSHOT="$(mktemp --suffix=.db)"
trap 'rm -f "$SNAPSHOT"' EXIT

# .backup is consistent even while the pollers hold the WAL open.
sqlite3 "$WW_DB_PATH" ".backup '$SNAPSHOT'"

restic backup "$SNAPSHOT" --tag worldwatch --host "$(hostname)"
restic forget --tag worldwatch --keep-daily 7 --keep-weekly 8 --prune
