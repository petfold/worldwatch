# worldwatch — roadmap

Milestone-based: each milestone has a definition of done, and it is done when
every item under it is met. Keep this file updated (mark items DONE with a
date).

**This file is an index.** The detail lives in `doc/`: the milestone plan in
[`doc/p0-implementation-plan.md`](doc/p0-implementation-plan.md), per-source
state in [`doc/tier1-onboarding-status.md`](doc/tier1-onboarding-status.md),
the architecture in
[`doc/worldwatch-architecture-v0.1.md`](doc/worldwatch-architecture-v0.1.md),
what the operator still owes in
[`doc/OPERATOR-TODO.md`](doc/OPERATOR-TODO.md), and the plain-language
narrative in [`doc/PROGRESS.md`](doc/PROGRESS.md). Nothing from those is
restated here.

---

## P0 — an unattended pipeline on one small VPS

Goal: ingest the 8 Tier-1 sources, maintain the geometric cascade, run Layer-0
models, write the surprise archive, deliver push alerts and a minimal map. No
Layer 1 yet.

### Sources — all eight live

Per `doc/tier1-onboarding-status.md`; "live" means a config stanza exists, the
poller fetches it, and a parser turns it into observations, verified against a
fixture and/or the real feed.

- [x] Seismic, USGS (all-hour summary feed)
- [x] Markets — crypto BTC/ETH via `coinbase_spot`
- [x] News events — GDELT, raw 15-minute export files
- [x] Wikipedia pageviews
- [x] Internet health — Cloudflare Radar, global plus GB/US/JP
- [x] Severe weather — NWS, US; zone-only alerts skipped
- [x] Radiation — Safecast
- [x] Night lights — Earthdata login verified 2026-07-11
- [ ] FX markets — the one source gap: crypto is live keyless, FX needs a keyed
      provider (🟡 in `OPERATOR-TODO.md`).

### Definition of done — not yet met

From `doc/p0-implementation-plan.md`. These gate P0 and none can be ticked from
the repository alone; they need the deployment.

- [ ] Runs 14 days unattended, surviving poller crashes, API outages and a VPS
      reboot.
- [ ] Surprise archive populated for every promoted source, PIT audit passing.
- [ ] At least one real-world event visibly flagged — seismic events guarantee
      this, which is why they are in the set: free ground truth.
- [ ] A push notification received on a phone for a corroborated (naive) alert.

### Deployment — the operator's half

- [x] Push channel decided (DONE 2026-07-11): self-hosted ntfy on the VPS.
- [ ] Provision the small VPS — Python 3.12+, systemd.
- [ ] Point the existing restic/B2 backup at the SQLite file and its WAL.
- [ ] Register any per-source accounts that need the VPS's own IP.
- [ ] Decide the data directory and retention — raw fine-window length, Parquet
      export policy.

## Layer 1 — after P0

- [ ] Explicitly out of scope for P0 ("No Layer 1 yet"). Scope it once P0's
      definition of done is met.

---

## Released

0.1.0 (2026-09-10) shipped the eight pollers and their per-source modelling;
see [CHANGELOG.md](CHANGELOG.md). Note that a released package is not the same
as a met milestone — P0's gates are about a running deployment, not a wheel.
