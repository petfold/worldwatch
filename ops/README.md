# Operations — running Worldwatch on a VPS

P0 runs as a few systemd units talking only through one SQLite file
(architecture §11): one long-running **poll** service, one **api** service, and
three timer-driven passes (**consolidate**, **detect**, **presence**).

```
poll (service) ── fetch → raw_ring → live score → surprise → alerts → push   (seconds)
consolidate (timer, 5m) ─────▶  bins (archive cascade; folds only scored rows)
detect (timer, 5m) ──────────▶  alerts ──▶ push   (safety-net sweep, idempotent)
presence (timer, 15m) ───────▶  silence rows
api (service) ── reads ───────  dashboard + /api
```

Scoring happens in the poll process as observations arrive (ADR 0002): each
stream at its native resolution, CUSUM evidence in memory, the alert policy and
the push in the same event loop. The detect timer re-applies the alert policy
to the surprise archive in case the live path missed anything.

## Quick start

```bash
sudo REPO_URL=<your-fork> ops/deploy.sh      # provisions user, venv, units, DB
sudoedit /etc/worldwatch/worldwatch.env      # set push channel + any API keys
sudo systemctl restart 'worldwatch-*'
```

`deploy.sh` is idempotent — re-run it to update code and units.

## The processes

| Unit | Type | Cadence | Does |
|------|------|---------|------|
| `worldwatch-poll.service` | service | continuous | one async poller per source → `raw_ring`; live Layer-0 scoring, alerts and push on arrival; closes count windows every 30 s |
| `worldwatch-consolidate.timer` | timer | 5 min | fold aged, scored rows → `bins` (archive) |
| `worldwatch-detect.timer` | timer | 5 min | alert sweep over the surprise archive → push |
| `worldwatch-presence.timer` | timer | 15 min | silence detection |
| `worldwatch-nursery.timer` | timer | 6 h | calibration audit: promote calibrated sources, quarantine drifted ones (ADR 0006; also run by `init`) |
| `worldwatch-api.service` | service | continuous | dashboard + read API (+ label write) |

All passes are idempotent and crash-safe, so timers can fire freely and a
restart mid-pass is harmless.

Every unit runs in `worldwatch.slice`: a 3.6 GB memory cap for all of them
together and half the default CPU/IO weight, so a co-hosted website keeps
priority and an OOM stays inside worldwatch. Adjust in
`ops/systemd/worldwatch.slice`.

## Public dashboard (read-only)

`ops/nginx/worldwatch-public.conf` serves the dashboard at `https://categor.io:8001`
with the site's existing certificate: GET only (alert labelling stays behind the
SSH tunnel), rate-limited, `noindex`. uvicorn itself keeps listening on
127.0.0.1:8001. Set `WW_DASHBOARD_URL=https://categor.io:8001` so pushes open the
alert on the dashboard; install steps are in the file's header.

## Storage budgets

| What | Kept | Setting |
|------|------|---------|
| bins / surprise | permanent (small) | — |
| `seen` keys (dedup across re-fetches) | 8 days | `WW_SEEN_RETENTION_SECONDS` |
| evidence store (what happened, for people) | fixed size, oldest out | `WW_CONTEXT_BUDGET_MB` (2048) |

SQLite reuses freed pages, so the file levels off near the budgets rather than
shrinking; `VACUUM` reclaims space if a budget is lowered.

## Manual invocation (debugging)

```bash
sudo -u worldwatch .venv/bin/python -m worldwatch init          # register sources
sudo -u worldwatch .venv/bin/python -m worldwatch consolidate
sudo -u worldwatch .venv/bin/python -m worldwatch detect
```

## Observability

The system instruments itself (P9) — every poll, pass, and push is a row in the
`health` table. Useful checks:

```sql
SELECT component, event, COUNT(*) FROM health GROUP BY 1, 2 ORDER BY 1;
SELECT * FROM alerts ORDER BY opened_at DESC LIMIT 20;
```

Resource use is data too (`worldwatch.usage`): every HTTP request of the poll
process is charged to its source (`usage`: bytes in/out and requests per source
per UTC day), and every 10 min the poll process samples the slice's memory and
CPU, its network bytes (`IPAccounting=yes` on `worldwatch.slice`), the host's
interfaces, free disk and the database size (`resources`). See it at
`/resources` (or `/api/resources`) and in the weekly digest. A warning is
recorded in `health` (component `resources`) and pushed at most once a day per
kind when memory passes 85% of the cap, free disk drops below 10 GB, downloads
exceed 3 GB/day, or one source takes over 25% of a day's downloads (above
100 MB) or over 1 GB/day. Limits: `WW_MEM_WARN_FRAC`, `WW_DISK_MIN_FREE_GB`,
`WW_BANDWIDTH_BUDGET_MB`, `WW_SOURCE_SHARE_MAX`, `WW_SOURCE_DAY_MAX_MB`.

```sql
SELECT component, bytes_in / 1e6 AS mb, requests FROM usage
WHERE day = strftime('%s', 'now') / 86400 * 86400 ORDER BY bytes_in DESC LIMIT 15;
```

## Backups: the daily Parquet export, pulled to the local machine

What lasts is exported; what rolls over is not. `worldwatch-export.timer` runs
`worldwatch export` at 00:30 UTC into `/var/lib/worldwatch/export/`
(`WW_EXPORT_DIR`):

- `surprise/` and `bins/`: a numbered batch a day with the rows written since
  the last one (~20 MB); nothing deletes them.
- `snapshots/`: the small permanent tables, whole (model states, alerts,
  pushes, digests, health, sources).
- Not exported: `raw_ring` (the fine window), `seen` (8 days), `context` (the
  evidence store's fixed budget).

The local machine pulls it daily with `ops/local/install-pull.sh` (a user
timer, no sudo): rsync over SSH into `~/worldwatch-archive`, never deleting
there, keeping the previous version of each rewritten snapshot for 14 days in
`.replaced/`. Read it with DuckDB: `ops/local/archive.sql` defines views that keep
the newest version of each row. Its outcome is data: `SELECT * FROM health
WHERE component = 'export'`.

A cloud copy is not set up. `ops/backup/restic-backup.sh` (a WAL-consistent
SQLite snapshot into restic) is ready for when one is.

## The 14-day soak (P0 definition of done)

Let it run unattended and check periodically that: it survives poller crashes
and API outages (Restart=always) and a reboot (timers Persistent=true); the
surprise archive fills for promoted sources; PIT stays calibrated; and at least
one real event (a sizeable earthquake is guaranteed) is visibly flagged. See
`doc/OPERATOR-TODO.md` for the human-side items (host, push channel, keys).
