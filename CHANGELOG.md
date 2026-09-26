# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

Started 2026-09-11, so 0.1.0's entry is written from the git history rather
than recorded at the time. Note that 0.1.0 was uploaded to PyPI directly rather
than by a `v*` tag push, so this repo has no release tags yet — unlike the
sibling projects, where the tag is what publishes.

## [Unreleased]

### Fixed

- **Co-located radiation detectors were mixed into one series** (EURDEP: 3,630
  stations at 3,369 coordinates). Radiation stanzas now geocode at H3 res 10
  and keep one detector per site (`site_field`).
- **API alert checks raced across processes** — check-then-insert now runs
  under BEGIN IMMEDIATE.
- **Bins were scored before they were complete.** Layer 0 scored each bin once,
  as soon as it appeared, while later raw rows could still fold into it (a
  GDELT bin scored at 5 events ended with 6; another at 1 ended with 8). Only
  closed bins are scored now: end + fine window + one pass ≤ now.
- **Cell details matched the day's top story to the day's peak surprise.** The
  peak now lists the records from its own bin ("no stories stored for that
  time" when there are none); the latest stories are listed separately.
- **Re-fetched observations were counted again after consolidation** — raw_ring's
  primary key only deduplicated inside the fine window, but pollers re-request
  overlapping history (USGS: the last hour, Wikipedia: 3 days, Cloudflare:
  7 days, NWS: every still-active alert). Once the first copy was folded into
  bins and deleted, each re-fetch landed again — on the first VPS day, 429
  quakes were binned where USGS lists 55. A new `seen` table (schema v4)
  remembers ingested (stream, cell, ts) keys for `WW_SEEN_RETENTION_SECONDS`
  (default 8 days, pruned by first sighting) and repeats are dropped at
  ingestion. Bins, surprise and Layer-0 state built before this are inflated
  and should be reset.
- **GDELT polling** — GDELT now 301-redirects `http://` to `https://`, and its
  `lastupdate.txt` still lists `http://` file URLs. The endpoint is `https://`
  and both requests follow redirects.
- **GDELT behind a stale CDN edge** — some Google CDN edges serve a cached empty
  404 for fresh export files. On a 404 the file is fetched straight from the
  GCS bucket behind `data.gdeltproject.org`.
- **API under concurrent requests** — a request's SQLite connection could be
  closed on a different threadpool thread than opened it (500s when the page
  fetched several endpoints at once).

### Added

- **Real-time detection path, phase 1 (ADR 0002).** Scoring moved into the poll
  process: each new observation is scored on arrival at its stream's native
  resolution (continuous: per observation; counts: windows bucketed by arrival,
  closed 60 s after they end, zeros included), the alert policy runs and the
  push goes out in the same event loop — seconds instead of 30–60 min. The
  detect timer is now an idempotent alert sweep; the cascade is archive only.
  Exactly-once via `raw_ring.scored` (schema v6), crash replay, and a
  consolidator that folds only scored rows.
- **Sequential evidence** replaces "≥ 2 anomalous bins": a CUSUM on each
  series' surprisal (k = 2, h = 4) — one reading at p ≤ 0.0025 alerts at once,
  weaker ones accumulate. Single-source streams need one reading at p ≤ q_tail;
  radiation now alerts on the first reading when 2 independent stations agree.
- **Per-source alert policy** (doc/adr/0001) — a stanza's `[alerts]` table:
  `role = "context"` (never corroborates; shown as news in the area),
  `single_source` (alert alone when ≥ `min_sensors` of the network's own
  stations agree past a stricter `q_tail`; nursery-capped at priority 4),
  `every_event` (authoritative feeds alert on each new item within one detect
  pass, without waiting for bins to close).
- **Radiation: EURDEP (~3,600 stations, ~40 countries) and German ODL (~1,600)**
  via BfS open data, hourly gamma dose rate, one station per fine cell;
  single-source. Parser support: `where` property filter, `transform = "log"`.
- **Authoritative alerts: USGS significant earthquakes, NWS Extreme** —
  `every_event`.
- **Evidence store — alerts say what happened.** Parsers keep a slim record per
  observation (per-stanza `[context]` fields): a quake's place, depth and USGS
  page; an NWS alert's type, severity and areas; a GDELT event's action,
  actors, place, mentions, tone, article link and a headline read from the
  URL slug. Stored in `context` (schema v5) under a fixed byte budget
  (`WW_CONTEXT_BUDGET_MB`, default 2 GB), oldest evicted first by the
  consolidator; never read by detection. Pushes list the top stories behind
  each signal with up to three tap-to-open buttons (source pages, map); the
  dashboard shows them on hover and in cell details. Guardrail 8 rewritten
  accordingly.
- **Public read-only dashboard** — `ops/nginx/worldwatch-public.conf` serves it
  at `https://categor.io:8001` (GET only, rate-limited, noindex); with
  `WW_DASHBOARD_URL` set, tapping a push opens that alert on the dashboard
  (`?alert=` / `?cell=` deep links).

### Changed

- **GDELT is context, not evidence, and counts articles.** News reports what
  sensors race to beat; it no longer corroborates alerts. It counts distinct
  articles per cell instead of coded events (~3.2 per article), which takes
  its dispersion from var/mean ≈ 7.9 to ≈ 1.3.
- **Layer 0 carries its own uncertainty (model v2, both flavors).** The count
  model was a plug-in EWMA negative binomial: after seeing 1 and 2 events it
  scored a 5 as 1-in-275 against Poisson(1.05), where the Bayesian predictive
  says about 1-in-15. It is now a dynamic Gamma–Poisson with a posterior over
  burstiness (NB size grid, Poisson included): the rate's Gamma posterior is
  discounted over `memory_seconds`, per-bin burst factors are integrated out,
  and the predictive mixes over dispersion by its posterior weight — wide when
  evidence is thin, narrow when ample, bursty when the stream proves bursty,
  with no warm-up special case. The continuous model now learns its noise
  variance (West & Harrison unknown-variance recursion, discounted, with state
  covariance in units of it); `obs_scale` is only a prior guess and the
  predictive's degrees of freedom grow with the evidence.
- **Readable pushes** — each evidence line now names the source, the rarity
  ("1-in-2,500 high") and what was observed in natural units ("3 quakes, max
  M6.6", "$84,000", "77% of 7-day peak"), with the region as lat/lon;
  tapping the notification opens the region on OpenStreetMap. Labels and units
  come from a new optional `[display]` table per source stanza. Context only:
  alerts still open on q_values alone.
- **Dashboard shows the data on calm days too** — a quiet "World now" list
  (per-source health, latest value, 24 h sparkline, the day's peak surprise),
  data dots on the map by modality, a legend, hover tooltips and click-through
  cell details; only unusual/rare hexagons are drawn. New read-only endpoints
  `/api/overview`, `/api/activity.geojson`, `/api/cell`; alerts carry their
  push text. Basemap is OpenFreeMap (zoomable, no key).
- **Resource limits for co-hosting** — every systemd unit runs in
  `worldwatch.slice` (3.6 GB memory cap, half CPU/IO weight), so a website on
  the same VPS keeps priority. `deploy.sh` installs the slice and restarts
  running services so a re-run picks up new code.

## [0.1.0] — 2026-09-10

First PyPI release: calibrated anomaly detection over heterogeneous world data
streams, aiming for lead time over the news.

### Added

- **P0 pollers — all eight Tier-1 feeds live**, each modelled on its own terms:
  GDELT news events from the raw 15-minute export files, Wikipedia pageviews as
  a count channel informing the model, BTC/ETH spot prices through `log1p` and
  a scale-sane model config, alongside the remaining Tier-1 sources.
- Test CI and the stack-wide `[test]` extra; BSD-3-Clause license declared in
  `pyproject`.
- PyPI packaging metadata: description, readme, project URLs and keywords.

### Changed

- **Renamed from `worldmonitor` to `worldwatch`** throughout — the repo URL,
  the deploy path, the HTTP User-Agent and the README title.
- Documentation: expanded README; the push channel decided as self-hosted ntfy
  on the VPS; a setup guide for Ubuntu 24.04 given the 3.12 requirement.

### Fixed

- `docs/` path references corrected to `doc/`, the directory that actually
  exists — the design documents had been linked from a path that did not.
- `.gitignore` covers `.env` (operator credentials) and restores rules that had
  been dropped.
