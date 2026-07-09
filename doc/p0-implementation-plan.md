# P0 Implementation Plan — Worldwatch first milestone

Goal: an unattended pipeline on one small VPS ingesting the 8 Tier-1 sources,
maintaining the geometric cascade, running Layer-0 models, writing the surprise
archive, and delivering push alerts + a minimal map. No Layer 1 yet.

Definition of done:
- Runs 14 days unattended; survives poller crashes, API outages, VPS reboot.
- Surprise archive populated for all promoted sources; PIT audit passing.
- At least one real-world event visibly flagged (seismic events guarantee this —
  that's why they're in the set: free ground truth).
- Push notification received on phone for a corroborated (naive) alert.

## Tier-1 sources for P0 (endpoint details to be researched at build time)

| # | Source                  | Flavor      | Cadence   | Notes                                   |
|---|-------------------------|-------------|-----------|-----------------------------------------|
| 1 | Seismic (USGS-style)    | events→count| 5 min     | also serves as evaluation ground truth  |
| 2 | Markets/FX/crypto       | continuous  | 5–15 min  | a handful of indices, majors, BTC/ETH   |
| 3 | News events (GDELT)     | events→count| 15 min    | geocoded counts per cell × category     |
| 4 | Wikipedia pageviews     | count       | hourly    | filtered project/page set; edit spikes later |
| 5 | Internet health         | continuous  | 15 min    | country/region traffic + outage feeds   |
| 6 | Severe weather alerts   | events→count| 15 min    | CAP-style feeds where free              |
| 7 | Radiation               | continuous  | 15–60 min | gov network(s) + citizen network        |
| 8 | Night lights            | continuous  | daily     | regional radiance aggregates            |

Registration needed for some (operator does this personally); config stanzas hold keys
via env vars, never in the repo.

## Build order (each step leaves a working system)

1. **Skeleton + store**: repo layout per CLAUDE.md; SQLite schema (below); config
   loader (TOML stanzas); logging that writes structured records into the DB
   (self-instrumentation from day one).
2. **Two pollers end-to-end**: seismic + Wikipedia (no auth, generous limits).
   Poller framework: async task per source, jittered schedule, retry/backoff,
   ETag/If-Modified-Since support, per-source health rows.
3. **Cascade**: consolidator folding raw_ring → bins with sketches; idempotent;
   property tests for octave math and re-run safety.
4. **Layer 0 continuous flavor**: Student-t seasonal state-space model, online update,
   PIT emission; calibration tests on synthetic generators. Wire to markets + radiation.
5. **Layer 0 count flavor**: negative-binomial seasonal model; wire to seismic counts,
   news-event counts, Wikipedia, weather alerts.
6. **Presence channel (minimal)**: expected-report model per source; presence_q emitted;
   silence rows appear in the archive. (Cause attribution/prober is P1 — for P0, tag
   poller-level failures vs source-level absence from poller metadata.)
7. **Remaining pollers**: internet health, night lights, remaining feeds.
8. **Naive alert engine**: per-stream persistence (N windows beyond quantile threshold,
   per scale) + naive corroboration (≥2 distinct modality classes anomalous in same
   cell-neighborhood & window) → alert rows.
9. **Push + map**: ntfy/Telegram notifier; FastAPI serving (a) GeoJSON of current
   per-cell max surprise, (b) per-cell timeline JSON; single-page MapLibre view with
   H3 cells + silence toggle + alert list.
10. **Ops**: systemd units + timers; restic backup of the SQLite file; deploy script;
    14-day soak.

## SQLite schema (DDL sketch — refine at build time)

```sql
CREATE TABLE sources (
  stream_id     TEXT PRIMARY KEY,
  class         TEXT NOT NULL,            -- e.g. 'radiation_station', 'city_transit'
  modality      TEXT NOT NULL,            -- physical|economic|infrastructural|informational
  topic_tags    TEXT NOT NULL,            -- JSON array
  flavor        TEXT NOT NULL,            -- continuous|count|rate|categorical
  config        TEXT NOT NULL,            -- JSON: endpoint, parse, geocode, cadence, auth ref
  status        TEXT NOT NULL DEFAULT 'nursery',  -- nursery|active|quarantined|retired
  created_at    INTEGER NOT NULL
);

CREATE TABLE raw_ring (                    -- fine window only; pruned by consolidator
  stream_id  TEXT NOT NULL,
  cell       TEXT NOT NULL,
  ts         INTEGER NOT NULL,
  value      REAL,                         -- NULL for pure-event rows
  meta       TEXT,                         -- JSON, minimal
  PRIMARY KEY (stream_id, cell, ts)
);

CREATE TABLE bins (
  stream_id  TEXT NOT NULL,
  cell       TEXT NOT NULL,
  scale      INTEGER NOT NULL,             -- octave index
  bin_start  INTEGER NOT NULL,
  n          INTEGER NOT NULL,
  vmin       REAL, vmax REAL, vmean REAL, m2 REAL,
  sketch     BLOB,                         -- t-digest
  PRIMARY KEY (stream_id, cell, scale, bin_start)
);

CREATE TABLE surprise (                    -- THE permanent archive
  stream_id     TEXT NOT NULL,
  cell          TEXT NOT NULL,
  scale         INTEGER NOT NULL,
  bin_start     INTEGER NOT NULL,
  q_value       REAL,                      -- PIT; NULL if no observation
  presence_q    REAL NOT NULL,
  precision     REAL NOT NULL,
  n_obs         INTEGER NOT NULL,
  tail_index    REAL,
  model_version INTEGER NOT NULL,
  PRIMARY KEY (stream_id, cell, scale, bin_start)
);

CREATE TABLE model_state (
  stream_id  TEXT NOT NULL,
  version    INTEGER NOT NULL,
  state      BLOB NOT NULL,                -- serialized state vector + params
  updated_at INTEGER NOT NULL,
  pit_stat   REAL,                         -- rolling uniformity statistic
  PRIMARY KEY (stream_id, version)
);

CREATE TABLE alerts (
  alert_id    INTEGER PRIMARY KEY,
  opened_at   INTEGER NOT NULL,
  status      TEXT NOT NULL,               -- open|acknowledged|resolved|false_positive
  severity    REAL NOT NULL,
  cell        TEXT NOT NULL,
  scale       INTEGER NOT NULL,
  evidence    TEXT NOT NULL,               -- JSON: [(stream_id, bin_start, q, precision),…]
  label       TEXT                          -- operator feedback for evaluation
);

CREATE TABLE health (                      -- self-instrumentation: pollers, allocator, api
  component  TEXT NOT NULL,
  ts         INTEGER NOT NULL,
  event      TEXT NOT NULL,                -- ok|http_error|timeout|parse_error|…
  detail     TEXT,
  PRIMARY KEY (component, ts, event)
);
```

## Key algorithms to implement (with acceptance criteria)

1. **Octave binning**: age-dependent bin width, ~8 bins/octave; function
   `bin_for(ts, now) -> (scale, bin_start)` and cascade-merge on aging.
   Accept: property tests — total order preserved, no gaps/overlaps, idempotent re-fold.
2. **t-digest merge** under consolidation. Accept: quantile error bounds on synthetic
   heavy-tailed data.
3. **Student-t seasonal SSM** (continuous flavor): state = [level, trend, harmonics],
   online update via robustified Kalman step; predictive distribution → PIT.
   Accept: on synthetic Gaussian+seasonal data, PIT uniform (KS test p>0.01 over 10k);
   on t(3) noise, level estimate not dragged by single 10σ spike (vs Gaussian baseline).
4. **Negative-binomial seasonal count model**: conjugate-style update with day-of-week/
   hour-of-day profile. Accept: PIT uniformity on synthetic Poisson/NB generators;
   correct flagging of injected count bursts.
5. **Presence model**: per-source Bernoulli/interval model of expected reporting with
   learned periodic gaps. Accept: scheduled nightly gap → no surprise; novel 3-interval
   silence → presence_q in tail.
6. **Rolling PIT audit**: windowed KS/χ² statistic per stream, persisted to model_state.
   Accept: detects a deliberately mis-specified model within N observations.
7. **Naive corroborated alerting**: persistence count per (stream, cell, scale) +
   modality-diversity check in cell neighborhood. Accept: replay of a recorded real
   earthquake day produces an alert; a single-stream spike does not escalate.

## Explicitly deferred to P1+

Layer-1 GGM; corroboration proper (precision-weighted evidence combination);
nursery auto-promotion (P0: manual promote after eyeballing PIT); prober & cause
attribution; allocator (P0: fixed cadences from config); GPD tail fits (P0: PIT from
the SSM predictive is enough); LLM anything; Parquet export & local-machine sync.

## First session with Claude Code — suggested opening tasks

1. Initialize repo per CLAUDE.md layout; pyproject; pre-commit (ruff, mypy).
2. Implement schema + migrations + config loader with one example stanza per flavor.
3. Implement octave-bin math with its property tests (pure functions, no I/O).
4. Implement the seismic poller against the live feed; observe rows landing in raw_ring.
Then proceed down the build order.
