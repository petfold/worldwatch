# Operator to-do — things only you can do

Actions that need a human (registrations, accounts, secrets, deployment). Claude
handles the code; these are the bits it can't and shouldn't do on your behalf
(guardrail 9: the operator registers any accounts personally; keys live in
environment variables, never in the repo).

Legend: 🔴 blocks a source/feature · 🟡 nice-to-have · ⚪ later (deploy phase)

## Accounts, keys & source decisions

- [ ] 🔴 **Internet health** — pick a provider and register. Cloudflare Radar is
      the richest free option and needs an API token. Once you have it, set it in
      an env var (e.g. `WW_CLOUDFLARE_TOKEN`) and tell Claude the var name so the
      stanza can reference it via `auth_env_var`. _(Blocks Tier-1 source #5.)_
- [ ] 🔴 **Night lights** — create a free NASA **Earthdata** login and decide
      which derived VIIRS/Black Marble regional-radiance product to ingest (not
      raw imagery — that's a non-goal). Provide credentials via env vars.
      _(Blocks Tier-1 source #8.)_
- [ ] 🟡 **GDELT news** — no account needed, but decide the access mode (raw
      15-min event files vs the DOC/GEO 2.0 query API). This is a design call for
      you to weigh in on; Claude will build the parser once the mode is chosen.
      _(Blocks Tier-1 source #3.)_
- [ ] 🟡 **FX markets** — crypto (BTC/ETH) is live without a key. If you want
      fiat FX pairs too, pick a free provider; most need an API key + env var.

## Push notifications (needed before step 9 is useful)

- [ ] 🔴 **Choose a channel**: self-hosted **ntfy** (preferred per your
      self-hosting preference) or Telegram. For ntfy: stand up / pick a server
      and topic. For Telegram: create a bot (@BotFather) and get the bot token +
      chat id. Provide via env vars.

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
