# CLAUDE.md — Worldwatch project guide

This file orients Claude Code (or any developer) on the Worldwatch project.
Read together with:
- `doc/worldwatch-architecture-v0.1.md` — the authoritative converged design spec
- `doc/global-anomaly-sources.md`       — data source catalog (tiers, principles)
- `doc/p0-implementation-plan.md`       — concrete first milestone
The spec is the source of truth. This file adds context, conventions, and guardrails.

## What this project is

A global anomaly/novelty detection system over free, open, heterogeneous data streams
(seismic, markets, news events, Wikipedia attention, internet health, weather alerts,
radiation, satellite night lights, later more). It learns the normal joint behavior of
the world's observable systems and flags calibrated deviations — with lead time over
mainstream news as the success metric. The learned coupling structure is itself a
primary scientific output ("the digital twin condenses out of the residuals").

## Core concepts (glossary — these words are load-bearing)

- **Layer 0**: one online Bayesian state-space model per source stream (level+trend+
  seasonal harmonics, Student-t noise; count flavor = negative binomial; categorical =
  Dirichlet–Markov). Fitted by Kalman-style recursions — NO training runs, NO GPU.
- **q_value / PIT**: the tail quantile of an observation under its Layer-0 predictive
  distribution. The ONLY interchange currency between layers. Never z-scores.
- **Presence channel**: per-source model of "did it report when expected". Missingness
  is data (missing-not-at-random). Never impute.
- **Geometric cascade**: multi-scale time bins, width ∝ age (~8 bins per octave),
  consolidation keeps count/min/max/mean/M2 + t-digest quantile sketch.
- **Surprise field / archive**: the permanent record of q_values per
  (stream, cell, scale, bin). Small (MB/year). This is the system's memory and the
  training data for Layer 1.
- **Layer 1**: ONE sparse dynamic Gaussian graphical model over the surprise field.
  Its sparsity pattern IS the coupling graph. Fitted offline (local machine), scored
  cheaply on the VPS.
- **Corroboration**: alerts require persistence × geographic coherence × ≥2 independent
  modalities agreeing on (place, time). Valid only because q_values are calibrated.
- **Nursery**: new sources run in shadow mode until their rolling PIT-uniformity test
  passes; then auto-promoted to alert-eligible.
- **Attention allocator**: per-source cadence tier {baseline|elevated|focus}; promotion
  pressure diffuses along coupling-graph edges (spatial AND topical); hard coverage
  floor so no source is ever unwatched.
- **Cell**: H3 hexagonal cell (resolution tied to scale) or named entity.

## Tech stack (decided — don't relitigate without cause)

- Python 3.12+, async pollers (httpx + asyncio), NumPy/SciPy for Layer 0
- SQLite (WAL mode) as the single store on the VPS; daily Parquet export;
  DuckDB + notebooks on the local machine for Layer-1 fits and backtests
- systemd units + timers for process supervision (pollers, consolidator, detector,
  allocator, api, prober)
- FastAPI + a single-page map dashboard (MapLibre + H3) for UI; ntfy or Telegram for push
- h3-py for spatial indexing; tdigest (or accumulator impl) for sketches
- No Kafka, no Postgres, no k8s, no message queues. SQLite until it measurably hurts.

## Repo layout (proposed)

```
worldwatch/
  doc/                   # the three docs above + ADRs as decisions accumulate
  src/worldwatch/
    config/              # per-source TOML stanzas (endpoint, parse, geocode, cadence,
                         #   topic tags, type hint) — adding a source = adding a stanza
    poll/                # async pollers, per-source isolation, retry/backoff
    ingest/              # parsers → normalized observations (field-drop at the door)
    cascade/             # geometric bin consolidator + sketches
    layer0/              # model family: continuous/count/rate/categorical flavors,
                         #   presence models, PIT audit, nursery, hierarchical priors
    layer1/              # (P1) sparse GGM fit + scoring
    alerts/              # persistence/coherence/corroboration engine
    allocate/            # (P2) attention allocator
    api/                 # FastAPI read-only endpoints + push notifications
    probe/               # active HTTP/DNS prober + cause attribution
  tests/                 # see testing section
  ops/                   # systemd units, deploy scripts, backup config
```

## Conventions & guardrails

1. **The interchange contract is sacred.** Layer 1 and above see only
   (stream, cell, scale, bin, q_value, presence_q, precision, tail_index,
   model_version). If a feature needs raw values upstream, the design is wrong.
2. **Every source is a ~10-line config stanza, not code.** If adding a source requires
   touching Python beyond a parser function, refactor.
3. **Per-source failure isolation.** One broken API must never stall other pollers.
   All poller errors are recorded as data (self-instrumentation), not just logged.
4. **Never impute missing data.** Predict-without-update; feed the presence channel.
5. **Calibration checks are tests.** PIT uniformity is monitored in production AND
   asserted in tests on synthetic data.
6. **Timestamps**: UTC epoch seconds everywhere internally. Bin boundaries computed,
   never stored ambiguously.
7. **Idempotency**: consolidator and detector must be safely re-runnable over the same
   window (crash-restart is the normal case).
8. **Field-drop at ingestion**: parse, keep the minimal record, discard the rest before
   it touches disk. Bandwidth/storage budget: 1–3 GB/day down, MB/day retained.
9. **Respect API terms**: honor rate limits, set a descriptive User-Agent, use
   conditional requests where supported. No scraping around auth walls; the operator
   registers any accounts personally.
10. **Keep it boring**: prefer stdlib + small deps; every new dependency needs a reason.

## Testing approach

- Unit: bin math (octave edges, ages), sketch merges, each model flavor's update step
  against closed-form/known results.
- Calibration: feed synthetic data from known generators (incl. heavy-tailed and
  seasonal) → assert PIT uniformity within tolerance; assert miscalibration IS detected
  when the generator and model disagree.
- Property/fuzz: consolidator idempotency and commutativity (re-run, out-of-order,
  duplicate observations); model state serialization round-trips.
- Replay: golden small fixtures of real feed payloads per source (checked in) → parser
  stability; a scripted historical replay harness is the backtest substrate (P1).
- Integration: single `docker compose`/venv run that polls 2 mock sources → cascade →
  Layer 0 → surprise rows → one synthetic alert, end to end.

## Milestones (detail in p0-implementation-plan.md)

- P0: 8 Tier-1 sources → cascade → Layer 0 → surprise archive → map + push alerts
      (naive corroboration). Runs unattended on a small VPS.
- P1: Layer-1 GGM, corroboration proper, nursery/PIT machinery, presence+prober,
      backtest harness.
- P2: allocator tiers + graph diffusion, LLM interpreter, fingerprints, Tier-2 sources.
- P3: deep Layer-1 v2 (behind lead-time acceptance test), LLM-as-sensor, EIG bandit,
      protocol write-up.

## Explicit non-goals

Webcams; social-media firehoses; raw imagery ingestion (on-demand tiles only);
commercial outage feeds; account creation on the operator's behalf; z-score-based
anomaly logic anywhere; imputation anywhere; heavyweight infra before it hurts.

## Operator context (useful when making choices)

- Deployment: cheap VPS (ingestion, 24/7) + Ubuntu local machine (research/fits).
- Operator values privacy and self-hosting; prefer self-hostable components (ntfy over
  proprietary push, etc.). Backups via restic/B2 exist already — plug into that.
- Long-term interest in publishing the coupling graph, lead-time results, and the
  calibrated-surprise protocol; keep code structured so the protocol layer could be
  extracted as a spec/library later. Decentralized persistence (Ethereum Swarm) may be
  used for archives/feeds later — keep storage behind a thin interface.
