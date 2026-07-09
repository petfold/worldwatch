# Operations — running Worldwatch on a VPS

P0 runs as a few systemd units talking only through one SQLite file
(architecture §11): one long-running **poll** service, one **api** service, and
three timer-driven passes (**consolidate**, **detect**, **presence**).

```
poll (service)  ── writes ──▶  raw_ring
consolidate (timer, 5m) ─────▶  bins
detect (timer, 5m) ──────────▶  surprise + alerts ──▶ push
presence (timer, 15m) ───────▶  silence rows
api (service) ── reads ───────  dashboard + /api
```

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
| `worldwatch-poll.service` | service | continuous | one async poller per source → `raw_ring` |
| `worldwatch-consolidate.timer` | timer | 5 min | fold aged rows → `bins` |
| `worldwatch-detect.timer` | timer | 5 min | Layer-0 score → alerts → push |
| `worldwatch-presence.timer` | timer | 15 min | silence detection |
| `worldwatch-api.service` | service | continuous | dashboard + read API (+ label write) |

All passes are idempotent and crash-safe, so timers can fire freely and a
restart mid-pass is harmless.

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

## Backups

`ops/backup/restic-backup.sh` takes a WAL-consistent SQLite snapshot and pushes
it to your existing restic/B2 repo. Schedule it hourly (cron or a timer):

```
RESTIC_REPOSITORY=... RESTIC_PASSWORD_FILE=... ops/backup/restic-backup.sh
```

## The 14-day soak (P0 definition of done)

Let it run unattended and check periodically that: it survives poller crashes
and API outages (Restart=always) and a reboot (timers Persistent=true); the
surprise archive fills for promoted sources; PIT stays calibrated; and at least
one real event (a sizeable earthquake is guaranteed) is visibly flagged. See
`doc/OPERATOR-TODO.md` for the human-side items (host, push channel, keys).
