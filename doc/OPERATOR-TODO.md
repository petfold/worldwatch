# Operator to-do — things only you can do

Actions that need a human (registrations, accounts, secrets, deployment). Claude
handles the code; these are the bits it can't and shouldn't do on your behalf
(guardrail 9: the operator registers any accounts personally; keys live in
environment variables, never in the repo).

Legend: 🔴 blocks a source/feature · 🟡 nice-to-have · ⚪ later (deploy phase)

## Accounts, keys & source decisions

- [x] 🔴 **Internet health** — done 2026-07-11: `WW_CLOUDFLARE_TOKEN` set and
      verified; `cf_radar_netflows_{global,gb,us,jp}` live (Radar netflows,
      15-min buckets). Remember to set the env var on the VPS too.
- [x] 🔴 **Night lights** — done 2026-07-11: Earthdata login verified
      (`WW_EARTHDATA_USER`/`WW_EARTHDATA_PASS`); product = VNP46A2 Black Marble
      daily gap-filled radiance, reduced to per-cell means at ingest (no raw
      imagery kept). 4 tiles live; note h17v03 (UK) has no retrievals near
      midsummer (physics, not a bug). Set the env vars on the VPS too.
- [x] 🟡 **GDELT news** — done 2026-07-11: you chose the raw 15-min event
      files; `gdelt_events` is live (no account needed).
- [x] 🟡 **FX markets** — done 2026-09-30: Open Exchange Rates free plan
      (hourly, 1,000 requests a month; a 304 counts too), `WW_OXR_APP_ID` set in
      `/etc/worldwatch/worldwatch.env` and checked; stanza `fx_usd`.

## Push notifications (needed before step 9 is useful)

- [x] 🔴 **Choose a channel** — decided 2026-07-11: **self-hosted ntfy on
      the VPS** (no Telegram; SimpleX considered as a possible later addition).
      Claude sets up the ntfy server + access token during the deploy; you just
      subscribe to the topic in the ntfy app afterwards.

## Deployment (step 10 — the soak)

- [x] ⚪ Provision the small VPS — done 2026-09-25 (categor.io; `ops/README.md`).
- [x] ⚪ **Backup** — done 2026-09-30: a daily Parquet export on the VPS,
      pulled to the local machine (`ops/local/install-pull.sh`; `ops/README.md`
      § Backups). No restic/B2 exists (the earlier note was wrong). A cloud copy
      later, with your general backup plan.
- [ ] ⚪ Register any per-source accounts flagged above on the VPS's IP if a
      provider ties keys to an origin.
- [x] ⚪ Data dir + retention — done 2026-09-26 (`/var/lib/worldwatch`, fixed
      budgets); Parquet export daily (2026-09-30).

## How to hand a secret to the system

1. You export the env var on the VPS (e.g. in the systemd unit's
   `Environment=` or an `EnvironmentFile=`).
2. The source stanza names it: `auth_env_var = "WW_CLOUDFLARE_TOKEN"`.
3. The poller reads it at fetch time via `SourceConfig.auth_token()`; the value
   never touches the repo or the database.

## Status pointers

- Per-source detail and what each remaining feed needs: `tier1-onboarding-status.md`
- Plain-language project status: `PROGRESS.md`
- Milestone index and what gates P0: `../ROADMAP.md`
