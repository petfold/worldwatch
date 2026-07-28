# worldwatch

[![tests](https://github.com/petfold/worldwatch/actions/workflows/tests.yml/badge.svg)](https://github.com/petfold/worldwatch/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-BSD--3--Clause-blue)](LICENSE)

**Monitor and model the world via APIs.**

worldwatch learns the normal joint behaviour of the world's observable systems —
seismic activity, markets, news events, Wikipedia attention, internet health,
weather alerts, radiation, satellite night lights — and flags *calibrated*
deviations from it. The target it is built against is **lead time over
mainstream news**.

The coupling structure it learns along the way is meant to be a primary output,
not a by-product: which systems move together, and how that changes. The design
note puts it as *"the digital twin condenses out of the residuals."*

## The idea in five points

- **Surprise is the only currency.** Every source gets its own model, and what
  leaves that model is a `q_value` — the tail quantile of an observation under
  its own predictive distribution. Nothing downstream ever sees raw values.
- **Therefore z-scores are excluded by design.** A z-score assumes a scale that
  heavy-tailed, seasonal, count-valued streams do not have. Quantiles under a
  per-source predictive distribution are comparable across a seismograph and a
  price feed; standard deviations are not.
- **Missing data is data, never imputed.** If a source fails to report when it
  was expected to, that goes to a separate *presence* channel and is modelled.
  Imputing it would manufacture the very calm the system is trying to detect the
  absence of.
- **One model per stream, fitted online.** Level + trend + seasonal harmonics
  with Student-t noise (negative binomial for counts, Dirichlet–Markov for
  categoricals), advanced by Kalman-style recursions. No training runs, no GPU —
  it is meant to hold a whole planet's worth of streams on a cheap VPS.
- **Calibration is what makes corroboration valid.** An alert requires
  persistence × geographic coherence × at least two *independent modalities*
  agreeing on the same place and time. That test only means something because
  the inputs are calibrated quantiles, so agreement across a seismic feed and a
  news feed is comparable evidence.

## Shape of the pipeline

```
  sources          one ~10-line TOML stanza each, never code
     │
     ▼
  poll ─────────► ingest ────────► cascade
  async, per-      parse, drop      geometric time bins (width ∝ age),
  source failure   fields at the    count/min/max/mean/M2 + t-digest
  isolation        door             sketches
                                       │
                                       ▼
                                  Layer 0
                        one online state-space model per stream
                        + a presence model per stream
                                       │
                                  q_values only
                                       ▼
                             the surprise field
                    the permanent record, and the system's memory
                              (MB per year, not GB)
                                   │        │
                    ┌──────────────┘        └───────────────┐
                    ▼                                       ▼
                 alerts                                 Layer 1
        persistence × coherence ×              one sparse dynamic Gaussian
        ≥2 independent modalities              graphical model, whose sparsity
                    │                          pattern *is* the coupling graph
                    ▼
              API + map + push
```

Space is indexed by H3 hexagonal cells (resolution tied to timescale) or by
named entity, so "the same place" is a well-defined join across modalities.

## Status — early, and honest about it

**The P0 spine runs end to end**: polling → parsing → the geometric cascade →
Layer 0 (continuous, count and presence flavours) → the surprise field →
alerting → a read-only API with push notifications, driven by a
`worldwatch` CLI and deployable with the systemd units under `ops/`. There are
**15 source stanzas covering the 8 Tier-1 modalities**, and the test suite runs
in CI on every push.

**Not started** — these are empty packages, not partial implementations:

| | |
|---|---|
| `layer1/` | the sparse GGM and the coupling graph. Today's alerting is the naive corroboration rule, not graph-informed. |
| `allocate/` | the attention allocator — per-source cadence tiers and promotion pressure diffusing along coupling-graph edges. |
| `probe/` | the active HTTP/DNS prober and cause attribution. |

Also still to come in P1: the nursery (new sources held in shadow mode until
their rolling PIT-uniformity test passes) and the historical replay harness that
the lead-time claim will have to be argued from. **No lead-time result is being
claimed yet** — the backtest substrate for making that argument doesn't exist.

## Quick start

```bash
pip install -e ".[test]"

worldwatch init          # register the configured sources
worldwatch poll          # fetch one round
worldwatch consolidate   # roll observations up the geometric cascade
worldwatch presence      # update the presence channel
worldwatch detect        # score and raise alerts
worldwatch api           # read-only API + map dashboard (127.0.0.1:8000)
```

Python ≥ 3.12. SQLite in WAL mode is the only store, with a daily Parquet
export for offline work; there is deliberately no Kafka, no Postgres, and no
message queue.

Six of the eight modalities need no credentials. Cloudflare Radar needs
`WW_CLOUDFLARE_TOKEN` and the night-lights tiles need
`WW_EARTHDATA_USER`/`WW_EARTHDATA_PASS`; both simply stay quiet without them.
See [`ops/worldwatch.env.example`](ops/worldwatch.env.example).

## Adding a source

A source is a config stanza in `src/worldwatch/config/sources/`, not code:
endpoint, how to parse it, how to geocode it, cadence, topic tags, and a type
hint. If a new source needs Python beyond a parser function, that is treated as
a signal to refactor rather than a normal cost.

## Docs

New here? [`doc/PROGRESS.md`](doc/PROGRESS.md) explains the whole system in
plain language, without the vocabulary.

- [`doc/worldwatch-architecture-v0.1.md`](doc/worldwatch-architecture-v0.1.md) — the authoritative design spec
- [`doc/global-anomaly-sources.md`](doc/global-anomaly-sources.md) — data source catalogue, tiers and selection principles
- [`doc/p0-implementation-plan.md`](doc/p0-implementation-plan.md) — the current milestone
- [`doc/tier1-onboarding-status.md`](doc/tier1-onboarding-status.md) — per-source status: what's live and what each remaining one needs
- [`doc/OPERATOR-SETUP-GUIDE.md`](doc/OPERATOR-SETUP-GUIDE.md) — running it yourself
- [`ops/README.md`](ops/README.md) — deployment
- [`CLAUDE.md`](CLAUDE.md) — conventions and the load-bearing glossary
