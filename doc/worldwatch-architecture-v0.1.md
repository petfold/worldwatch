# WORLDWATCH — Architecture & Specification v0.1
Global anomaly detection over heterogeneous open data streams.
Status: converged design, 2026-07-09. Companion file: global-anomaly-sources.md (data source catalog).

---

## 1. Purpose

Detect substantial global or regional events — novelty, unusual patterns, early-warning signals —
by learning the normal joint behavior of many open data streams and flagging calibrated deviations
from it. Secondary (and scientifically primary) product: the learned structure itself — an
empirical map of how the world's observable systems couple.

Named outputs:
1. Alerts with lead time over mainstream coverage
2. The coupling graph: which streams predict which, at what lags and scales
3. Event fingerprints: empirical taxonomy of multi-stream event signatures
4. Lead-time science: which sources are early for which event types
5. The protocol: a calibrated-surprise interchange format others can implement

## 2. Design principles (agreed)

- P1. Anomaly = surprise under an explicit model; every level names its model.
- P2. Hierarchical predictive coding: each layer models what it can from its own scope and
      passes UP only calibrated prediction errors (surprise), never raw data.
- P3. Interchange currency = tail probability (quantile / PIT value), NOT z-scores.
      Honest under any distribution shape; heavy tails are first-class (Student-t noise,
      EVT/generalized-Pareto for extremes, quantile sketches in storage).
- P4. Fanciness strictly increasing with height: free online Bayesian models at the bottom,
      one sparse interpretable model in the middle, LLM at the top (event-driven only).
      Formally: EBMs restricted to the tractable normalized regime; local license to go
      unnormalized (with conformal calibration) where a specific stream demands it.
- P5. Absence is data: presence channel per source, modeled like any other stream.
      Never impute. Missing-not-at-random is an alert pathway, not a nuisance.
- P6. Calibration is non-negotiable and self-auditing (PIT uniformity tests).
- P7. Few models: ONE Layer-0 family (few observation flavors), ONE Layer-1 model,
      ONE LLM. Thousands of instances, three model classes.
- P8. Attention is a budgeted resource allocated by expected information gain,
      propagating along the coupling graph (spatial + topical edges), with a hard
      coverage floor against streetlight blindness.
- P9. The system instruments itself: pollers, allocator decisions, and its own health
      are sources like any other, so world-going-quiet is distinguishable from
      system-going-deaf.
- P10. Boring infrastructure: systemd + SQLite until something measurably hurts.

## 3. System overview

```
sources (open APIs/feeds)
   │  pollers (async, per-source isolation, cadence set by allocator)
   ▼
raw ring buffer (fine resolution, short retention)
   │  consolidator (geometric bin cascade + quantile sketches)
   ▼
LAYER 0 fleet: per-(stream) robust seasonal Bayesian state-space models
   │  emits: (stream, cell, scale, bin) → q_value, presence_q, precision
   ▼
SURPRISE FIELD ARCHIVE (permanent, small)  ←— the system's memory
   │
LAYER 1: sparse dynamic graphical model over the surprise field
   │  emits: joint surprise, correlation-break scores, coupling graph
   ▼
ALERT ENGINE: persistence × geographic coherence × cross-source corroboration
   │
LLM INTERPRETER (event-driven): hypothesis narrative, fingerprint match
   ▼
UI: push alerts (primary) + map/timeline dashboard + silence map

ATTENTION ALLOCATOR: reads surprise field + coupling graph + alert state,
sets per-source cadence tier {baseline | elevated | focus}, logs itself.
```

Deployment split: small VPS (pollers, consolidator, Layer 0, alert engine, UI server,
compact store) + local machine (Layer-1 fits, backtests, research; syncs archive, MB/day).
Degradation mode: last-known picture + silence map.

## 4. Layer 0 — source models

Family: linear state-space model with level + trend + seasonal harmonics
(daily/weekly/annual as detected), STUDENT-T observation noise, fitted ONLINE by
Kalman-style recursions (constant time/memory per observation; no training runs, no GPU).

Observation flavors (config- or inference-selected, not separate codebases):
- continuous  (Student-t observation model)
- count       (negative binomial; conjugate-ish updates)
- rate/fraction
- categorical-state (Dirichlet–Markov transition model; surprise = transition improbability)
Extreme tail: peaks-over-threshold with generalized-Pareto fit per stream above a high
quantile; provides tail-index estimate (also a published output) and honest surprise
for the region beyond seen data.

Non-numeric reductions:
- events → counts per (cell, scale, category)
- categorical states → transition surprise
- text → (v2) LLM-as-sensor extracting (what, where, severity) tuples → count pathway;
  or embedding-novelty with conformal calibration. Same output contract.

Presence channel: per source, a companion model of "did it report when expected"
(learns maintenance windows, nightly gaps, flakiness). Below it, cause attribution:
{poller-fault | network-fault | station-absent} via active prober + self-monitoring,
so self-anomalies don't masquerade as world-anomalies.

Hierarchical priors: sources grouped by class (e.g. "city transit", "radiation station");
new instance cold-starts from class posterior, localizes with data.

Onboarding pipeline (automatic after ~10-line config stanza):
1. config: endpoint, parse rule, geocode, expected cadence, topic tags, type hint (optional)
2. type + seasonality inference from first days (support, discreteness, overdispersion,
   spectral peaks)
3. cold start from class prior
4. NURSERY: shadow mode, no alert contribution, until rolling PIT uniformity test passes
   → automatic promotion.

Self-audit: rolling PIT uniformity per model; drift ⇒ flag for refit/quarantine.

## 5. Interchange record (the load-bearing contract)

SURPRISE record — one per (stream, cell, scale, bin):
  stream_id        text     — source instance
  cell             text     — H3 cell (resolution tied to scale) or entity id
  scale            int      — octave index in the geometric cascade
  bin_start        int      — epoch seconds
  q_value          real     — PIT tail quantile of observed value under predictive dist [0,1]
                              (two-sided encoded as min(q, 1−q) with sign, or store raw PIT)
  presence_q       real     — quantile under presence model (expected-report likelihood)
  precision        real     — weight; reflects model confidence + source reliability
  n_obs            int      — observations folded into this bin
  tail_index       real     — current GPD shape estimate for this stream (context)
  model_version    int      — Layer-0 parameter version that produced this
Notes: q_values are the ONLY thing Layer 1 ever sees. Historical q_values are frozen
(cannot be perfectly recomputed under later model versions — accepted).

## 6. Storage

- raw_ring:      fine-resolution observations, retention = fine window only (e.g. 48 h)
- bins:          geometric cascade, ~8 bins per octave of age; per bin store
                 count, min, max, mean, M2 (variance), and a t-digest quantile sketch BLOB
- surprise:      the archive above — PERMANENT, primary record (~MB/year per 1000 streams)
- presence:      same shape as surprise (or folded in as presence_q)
- model_state:   per stream: current state vector + versioned parameters (tiny)
- alerts:        see §8
- attention_log: allocator decisions as a first-class stream (P9)
- cold archive (optional): raw compressed feeds for selected high-value sources → B2
Engine: SQLite (WAL). Export: daily Parquet snapshots → local machine (DuckDB for analysis).

## 7. Layer 1 — coupling model

v1: sparse dynamic Gaussian graphical / factor model over the transformed surprise field
(probit/Gaussianized q_values), fitted on the local machine, parameters shipped to VPS.
- Precision-matrix sparsity pattern = THE COUPLING GRAPH (deliverable #2).
- Scores: joint surprise of current field; correlation-break score (streams that
  normally co-move, decoupling); explained-away discount (one storm ≠ three anomalies).
- Refit cadence: slow (daily/weekly); scoring on VPS is cheap linear algebra.
Prior edges: topic tags + spatial adjacency seed the graph; learned edges refine/replace.
v2 (acceptance test = measured lead-time gain over v1): deep sequence model
(graph transformer / SSM / JEPA-style compatibility scorer with conformal calibration)
predicting the next surprise field. v1 is retained as the explanation layer regardless.
Fingerprints: cluster historical alert episodes in signature space (which streams,
what order, what scales) → empirical event taxonomy; new alerts get nearest-fingerprint
classification.

## 8. Alert engine

Escalation requires ALL of:
- persistence: anomaly holds N consecutive windows at its scale
- geographic coherence: neighboring cells / related entities agree
- cross-source corroboration: ≥2 independent modalities (physical/informational/economic/
  infrastructural) implicate same (place, time) — combined via precision-weighted
  evidence, valid because q_values are calibrated (P6)
Presence anomalies participate identically (regional multi-source silence = loud alarm).
Alert record: severity, region, scale, contributing (stream, q, precision) list,
fingerprint match, status lifecycle (open/ack/resolved), links to evidence.
Multi-scale: detectors run per octave — fine scales catch shocks, coarse scales catch
slow drifts; novelty at ANY scale is reportable.

## 9. Attention allocator

Budget: bandwidth + per-API rate limits + poll slots.
Cadence tiers per source: baseline | elevated | focus (e.g. 1× / 4× / 16× baseline rate).
Promotion pressure diffuses from surprising nodes ALONG COUPLING-GRAPH EDGES:
- spatial edges: anomaly in cell ⇒ boost sources in/near cell
- topical edges: anomaly in a stream ⇒ boost same-topic streams globally AND its learned
  causal neighbors (tags = prior, learned couplings = posterior)
- user zoom (geographic OR topical) ⇒ temporary focus tier for the selected slice
- unresolved multi-alert ⇒ focus + on-demand pulls (e.g. imagery tiles) as extreme case
Demotion: exponential decay back to baseline.
Guards: HARD coverage floor (no source ever below minimum cadence); allocator decisions
logged as a stream so self-inflicted darkness is attributable (anti-streetlight, P8/P9).
v2: replace tier rules with expected-information-gain bandit.

## 10. LLM layer

Strictly ABOVE the detection loop; event-driven or slow per-region cadence; never scores.
Input: structured evidence (contributing surprises, coupling context, fingerprint match,
recent geocoded news). Output: hypothesis paragraph, resemblance statement, suggested
next observations. Also (v2) LLM-as-sensor for text streams (separate Layer-0 citizen).

## 11. Processes (VPS, systemd-supervised)

- pollers        async, per-source task isolation, retry/backoff, health-instrumented
- consolidator   idempotent fold into cascade, every few minutes
- detector       per consolidation cycle (fine scales) / hourly+ (coarse); writes alerts
- allocator      reads surprise+alerts+graph, writes cadence table, logs itself
- api/ui         read-only dashboard + push notifications (ntfy/Telegram/email)
- prober         active HTTP/DNS checks (service-layer sensor + cause attribution)
All communicate through the database. Local machine: layer1-fit, backtest, research
notebooks; rsync/parquet sync of archive.

## 12. UI

1. Push alerts (primary): severity, region, one-liner, contributing signals, link.
2. Dashboard: world map (H3 cells colored by current surprise; silence-map toggle),
   alert feed, click-through to per-cell timeline showing each stream vs its predictive
   band; zoomable timeline mirrors geometric bins (fine near now, coarse in the past).
   Zoom (geo or topic) feeds the allocator (§9).
3. Alert view: evidence list, fingerprint match, LLM hypothesis, feedback buttons
   (true/false/unclear → labels for evaluation).

## 13. Evaluation

- Ground truth: Wikipedia Current Events + GDELT records + curated incident list.
- Primary metric: lead time vs mainstream coverage, per event type per source.
- Secondary: false-alert rate at fixed sensitivity; PIT calibration health per source;
  coverage-floor compliance.
- Method: historical replay/backtests on the archive; alert feedback labels.

## 14. Phasing

- P0 (weeks):  Tier-1 sources (seismic, markets, news events, Wikipedia, internet health,
               weather alerts, radiation, night lights) → pollers → cascade → Layer 0 →
               surprise archive → map + push alerts. No Layer 1 yet: alert on
               single-stream persistence + naive corroboration (count of independent
               modalities over threshold).
- P1:          Layer-1 GGM + explained-away logic + corroboration proper; backtest
               harness; nursery/PIT machinery; presence channel + prober attribution.
- P2:          allocator tiers + coupling-graph diffusion; LLM interpreter; fingerprint
               clustering; Tier-2 sources (ADS-B, grid, GTFS-RT heartbeats, trace gases,
               CAP, air quality, water).
- P3:          deep Layer-1 v2 behind lead-time acceptance test; LLM-as-sensor;
               EIG bandit allocator; protocol write-up.

## 15. Open parameters (deliberately deferred)

bins-per-octave (default 8); fine-window length; H3 resolution per scale; nursery
pass criteria; persistence N per scale; corroboration threshold; tier multipliers;
coverage-floor cadence; GPD threshold quantile; refit cadences.

## 16. Non-goals / exclusions

Webcams (privacy/legal); social-media firehoses (access); raw imagery ingestion
(on-demand only); per-service commercial outage data; account creation on behalf of
the operator; real-time claims about municipal drinking water (known blind spot).
