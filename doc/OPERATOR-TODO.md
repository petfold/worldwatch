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
- [ ] 🟡 **FX markets** — crypto (BTC/ETH) is live without a key. If you want
      fiat FX pairs too, pick a free provider; most need an API key + env var.

## Push notifications (needed before step 9 is useful)

- [x] 🔴 **Choose a channel** — decided 2026-07-11: **self-hosted ntfy on
      the VPS** (no Telegram; SimpleX considered as a possible later addition).
      Claude sets up the ntfy server + access token during the deploy; you just
      subscribe to the topic in the ntfy app afterwards.

## Deployment (step 10 — the soak)

- [ ] ⚪ Provision the small VPS (Python 3.12+, systemd). Claude will supply the
      systemd unit + timer files; you install and `systemctl enable` them.
- [ ] ⚪ Point the existing **restic/B2** backup at the SQLite file + WAL (you
      already have restic/B2 — this just adds the DB path and a schedule).
- [ ] ⚪ Register any per-source accounts flagged above on the VPS's IP if a
      provider ties keys to an origin.
- [ ] ⚪ Decide the data dir + retention (raw fine-window length, Parquet export
      cadence to the local machine).

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
