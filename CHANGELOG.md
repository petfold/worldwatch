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

- **387 pushes in one day (2026-09-27; 8 the day before).** 370 were the
  reachability prober's: its probes "failed" in up to 43 countries at once
  (09:20-09:45 UTC), a fault at our end, and each country was re-alerted every
  2-minute round (~7 alerts each), because a single-source alert was suppressed
  only by one opened after the current evidence's window, which moves on each
  round; IODA re-reported one ongoing outage every 30 minutes. Now:
  - single-source streams alert once per region per episode: none again within
    `cooldown_seconds` (default 6 h);
  - `max_regions` in a single-source stanza: more regions alerting at once is
    taken for our vantage point, a `vantage_suspect` health record instead of
    alerts (the prober: 5);
  - every-item feeds may set `cooldown_seconds` too (IODA: 6 h);
  - a push budget: an alert is pushed only if it is among the most serious,
    by `alert_score` (how improbable its evidence is, −log10 p over independent
    cells, times the number of modalities confirming it; every-item feeds by
    their stanza's `push_score`): at least `WW_PUSH_MIN_SCORE` (8) and the
    week's (`WW_PUSH_PER_DAY` × 7)-th highest score, so about 2 a day, and never
    more than twice that in 24 h. The rest stay on the dashboard (a `push_log`
    table, schema v9).
  - priority 5 (the one that may wake the operator) only for extreme alerts:
    confirmed by two modalities (or a stanza marked `extreme`) and scoring at
    least `WW_PUSH_EXTREME_SCORE` (15), at most `WW_PUSH_EXTREME_PER_WEEK` (1);
    every other push at priority 3 at most.
  Replayed over the alerts so far (395 in about 20 hours): 3 pushes, no
  wake-up.

### Added

- **Early alerts that escalate.** A stream that needs corroboration no longer
  stays silent until a second modality agrees when one reading is strong
  enough on its own (`alert_score` ≥ 6, p ~ 1e-6; on the archive, a few times
  a year): it opens an unconfirmed alert at once (the vantage guard applies, 5 regions by default). For 6 h
  after opening, each detection run gathers the region's new candidates into
  an open alert's evidence and raises its stage: unconfirmed (one modality),
  confirmed (two or more), extreme (confirmed, and `WW_PUSH_EXTREME_SCORE`).
  The push follows: "Unconfirmed" at priority 3 (skipping the week's ranking, not the 24-h cap), then an
  "(update)" push at the higher priority — "Confirmed" 4, "EXTREME" 5 (the one
  that may wake; then 4 once the week's allowance is used) — which skips the
  budget, at most once per stage. An alert held at first is pushed when
  confirmation lifts it into the budget. Schema v9 adds `alerts.stage`,
  `alerts.escalated_at` and `push_log.stage`.
- **Reach: only what can affect you wakes you.** `WW_HOME` (one or more
  places, in the server's env file only) and a `reach_km` per stanza's
  `[alerts]`: km from the event's cell (quakes 300, weather warnings 200,
  GDACS 500, a country's internet 300), or `"global"`. With `WW_HOME` set, an
  extreme alert wakes (priority 5) only if an event in it reaches one of your
  places; otherwise it is pushed at 3 at most, "far away" (no distance: the
  push must not locate you). Streams with no reach (attention, news, markets)
  never make an event near. Radiation networks reach 500 km, or everywhere
  when 5 or more stations agree (`global_sensors`: a release, not a
  detector).
- **Authoritative sources can be extreme on their own.** `extreme = true` for
  GDACS red (now `push_score` 15) and the radiation networks (independent
  stations confirm each other). Until now nothing but two modalities could
  wake, so neither a cyclone heading for home nor a radiation release could.

### Fixed (unreleased features)

- The escalation pass let news candidates confirm an alert; news is context,
  never evidence (ADR 0001).

### Added

- `WW_PUSH_SILENT_UNTIL` (an ISO date/time, UTC, or epoch seconds): until then
  every push goes out at ntfy priority 2 at most (no sound, no vibration;
  still listed).

- **Coinbase streams dropped together every hour or two** ("keepalive ping
  timeout"; our event loop was verified running, and reconnects met
  "connection reset by peer"): Coinbase doesn't answer pings reliably. Its
  streams now turn pings off and use Coinbase's 1/s heartbeat channel,
  reconnecting after 60 s of silence (`ping_interval`, `stale_seconds`).
- **An HTTP 204 was a fetch error** — "no matching events" (GDACS) is now an
  empty result.
- **NWS alerts sharing an onset merged into one key** — `id_field` keeps them
  distinct.
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

- **Dashboard: "only in view" and rows that go somewhere.** A toggle limits the
  World Now list and the alerts to the visible map area, following pans and
  zooms; sources not tied to a place (prices, world traffic, Wikipedia) stay
  listed in their own group. Clicking a row opens its details inline under the
  row (a second click closes) and moves the map — to the peak if it is
  unusual, else to the extent of what the source reported. `/api/overview`
  and `/api/alerts` take `bbox=west,south,east,north` (world copies and the
  antimeridian handled); overview rows carry `center`, `extent`, `global`.
- **Transparency page** at `/about` (linked from the map): what Worldwatch is,
  exactly what the prober sends (NTP client requests to pool servers every
  2 min; TCP connects to RIPE Atlas anchors), what is kept, how to opt out
  (GitHub issues), and attribution for every data source. The prober honours
  an `exclude` list of addresses/ranges.
- **Coverage balance (ADR 0002 phase 4).** Each region gets comparable weight,
  not weight proportional to how much data exists there: NWS thinned to
  Severe/Extreme in coarser cells (2.4 MB → 60 KB a poll, so every 2 min);
  earthquake detection on a uniform M4.5+ (`usgs_m45`, `emsc_m45`) with M1+
  kept as context; new non-US sources GDACS (Red wakes, Orange silent; an
  escalation re-alerts) and MeteoAlarm (37 European countries, orange/red
  warnings per country, via a new `multi_get` fetcher); a stream contributes
  at most 3 cells to an alert and 5 dots per region to the map; and a map
  layer of countries where our own probing is thin or absent.
- **Internet outages, fast and broad (ADR 0002 phase 3).** IODA's outage events
  (authoritative, every new event alerts at priority 4) and per-signal alerts
  (a count stream per country), plus our own active prober
  (`probe_reachability`): NTP pool country-zone servers and RIPE Atlas
  anchors, a quota per country across networks, every 2 min. Failures are a
  per-country count scored live; mislocated targets are rejected by the speed
  of light, and rounds where most targets fail everywhere are recorded as our
  own network's fault. Target IPs never leave the prober's table. Country
  points from Natural Earth (`config/countries.csv`).
- **Push feeds (ADR 0002 phase 2).** `[fetch] kind = "websocket"` stanzas stream
  instead of polling: Coinbase ticker for BTC/ETH (thinned to one observation a
  minute, or at once on a ≥ 0.3% move) and EMSC real-time earthquakes (new
  `emsc_seismic`, with the region and event page as context). Presence
  heartbeats while connected, reconnect with backoff. USGS feeds and NWS
  Extreme are polled every 60 s.
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

- **Safecast retired** — its endpoint serves stale data (newest January 2026);
  EURDEP and BfS supersede it.
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
