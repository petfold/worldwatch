# Worldwatch Project — Full Conversation Archive
Complete design discussion, 2026-07-09. Participants: Peter (operator/researcher), Claude.

---

## Opening: the vision

**Peter**: I saw some recent data dashboards showing global events, sometimes on a map. They're a step forward for getting an overview. Official news often take a long time to react; dashboards may be more up-to-date. But such dashboards still show raw data, pulled mostly from APIs. What I have in mind is more ambitious: a digital twin model of the world — an actual model. Machine learning would help find the structure and parameters. It could include as many data sources and APIs as practically possible. Global economic and market data would be in there, but also flight, shipping, traffic, possibly webcams and even Washington pizza orders. I'd be mainly interested in detecting substantial global events, any novelty, unusual patterns worth attention. Does this make sense and how would we do a more detailed plan?

**Claude**: Yes, it makes sense. Most pieces exist in isolation — GDELT monitors news events, BlueDot caught COVID days before WHO by fusing flight data with health chatter, hedge funds trade on satellite parking-lot imagery. What doesn't exist is the fusion layer: a system learning normal joint behavior and flagging deviations. One reframing: drop the literal "digital twin" interpretation. You need a learned model of normality that's calibrated about its own surprise, not a mechanistic world simulation.

Architecture proposed: five-layer system with hierarchical design, normalization at the bottom, multi-stream fusion learning in the middle, anomaly scoring and corroboration, then alerts and explanation.

---

## Design conversation 1: Models and hierarchy

**Peter**: Let's step back. Anomaly detection assumes some kind of model. When we talk about detecting novelty or anomaly, something is anomalous with respect to expected behaviour. And expectation is a kind of model. Should the individual sources have source-specific models, or should source processing be minimal, leaving all modelling to a higher model looking at many sources? Maybe we need a combination.

**Claude**: You're exactly right — anomaly detection without a model is incoherent. The spectrum is:
1. Marginal models (each series' own distribution)
2. Dynamical models (seasonality, trends per series)
3. Joint latent models (shared state across streams)
4. Structured/causal models (entities and mechanisms with learned parameters)

The answer is **hierarchical predictive coding**: each layer models what it can, and passes UP only prediction errors (surprise), not raw data. Layer 0 is thousands of models each seeing one stream — simple, robust, debuggable. Layer 1 is one model seeing everything — that's where interesting structure lives.

The interchange format is calibrated surprise: **every Layer 0 model, whatever it is inside, emits a calibrated predictive distribution** (hence surprise + precision) on a common grid. A Kalman filter on grid frequency and a Poisson model on protest counts become interchangeable.

On Bayesian vs. fancy models: simple-and-Bayesian at the bottom (data is thin, calibration is sacred) because conjugacy and linear-Gaussian structure keep it analytic. Structured-and-sparse in the middle (the parameters are the product). Deep only where measured lead time justifies it.

---

## Design conversation 2: Heavy tails and the interchange contract

**Peter**: The world is full of long-tailed, non-Gaussian distributions, especially important for disasters. We'll need statistics that give justice to long tails.

Also: transformers and LLMs at the top make sense. A high-level summary of a geographical region in human language is valuable. Keep things simple at the lower levels — not too many models. Low-level models are computationally intensive; simpler models are needed there. And: missing data itself can be ominous. Some patterns of missing data are very significant. There should be a mechanism of worrying about why the data is missing.

**Claude**: These force real design changes:

1. **Heavy tails change the interchange currency.** The common currency should be **tail probability under the source's own predictive distribution** (equivalently, a p-value / quantile position / probability integral transform), never a standardized deviation. Quantiles are honest under any distributional shape. Use Student-t noise in the models (robustness), keep quantile sketches in storage (t-digest), and use extreme value theory (generalized-Pareto fits to exceedances) for tail extrapolation.

2. **Transformers and LLMs at the top — yes, and it's more principled than it sounds.** They sit ABOVE the detection loop, event-driven, never inside it. When an alert fires, an LLM receives structured evidence and produces a hypothesis narrative. Never let it score — everything below must emit honest probabilities that compose. Later, LLM-as-sensor reading news text is a v2 Layer 0.

3. **Model minimalism and training cost solve each other.** One Layer 0 model family (robust seasonal state-space, a few observation flavors selected by config). One Layer 1 model (sparse graphical). One LLM (on-demand). The thousands of Layer 0 *instances* use online-fitting Kalman recursions: constant time, no GPU, no training pipeline. A new source is contributing within days.

4. **Missingness — promote absence to first-class observed variable.** Never impute. Every source emits two channels: the value channel and a **presence channel** — binary stream of "did it report when expected." Missingness has rich normal structure; the presence model learns it. Only deviations from predictable missingness generate surprise. "One station dark while internet-health also dips" is correlated presence anomaly with a plausible cause.

---

## Design conversation 3: EBM vs. Bayesian

**Peter**: Could it not all be energy-based models (up to the LLM)? Would we lose too much?

**Claude**: Short answer: you could, you'd lose less than expected — but what you'd lose sits at load-bearing points.

Dissolution: every probabilistic model IS an EBM (E(x) = −log p(x)). The real question is whether to use unnormalized, learned energies instead of the normalized corner.

What survives: calibration. Rank each observation's energy against the model's historical distribution; that empirical quantile is a distribution-free p-value. Conformal calibration rescues the interchange currency.

What doesn't survive:

- **The free bottom layer.** Unnormalized EBMs need contrastive/score-matching training — gradient descent, negative sampling, learning-rate tuning, per stream, times thousands. Kalman-style recursions fit online, closed-form, microseconds, forever, no training pipeline.
- **The generative side-benefits.** A normalized SSM gives multi-step forecasts with widening uncertainty, native missing-data handling, hierarchical priors for cold-starting. An energy function scores; it doesn't propagate a state.
- **The readable middle.** Layer 1's sparse precision matrix IS the coupling graph — the scientific product. A learned neural energy would likely score better but isn't readable.

Verdict: go fully unnormalized and you trade free online fitting, native missingness, cold-start pooling, and the interpretable graph, for distributional flexibility the quantile interchange format has already made mostly unnecessary. Restrict to the tractable normalized regime, with an explicit license to go unnormalized locally where a specific stream demands it.

---

## Sources conversation: inventory and strategy

**Peter**: Let's get a list of kinds of data sources, categories not individual sources. E.g. public transport real-time data, instead of listing all cities.

**Claude**: Category-level inventory:

**Movement**: flight tracking, ships/vessels, road traffic, public transport (GTFS-RT), border crossing, bike-share.

**Economy**: equity indices, FX, crypto, commodities, freight rates, CB stats, corporate filings, job postings.

**Information/attention**: news events, search trends, Wikipedia views/edits, social media (restricted), app rankings, DNS certs.

**Infrastructure**: internet health/outages, power grid, night lights, GPS interference, undersea cables.

**Environment**: earthquakes, volcanoes, weather, wildfires, floods, air quality, space weather.

**Health**: disease surveillance, wastewater, excess mortality.

**Governance**: armed conflict/protests, sanctions, legislation, elections, humanitarian data.

**Physical proxies**: satellite derivatives, NOTAMs, maritime warnings, venue activity anomalies.

Tier 1 (start here): seismic, markets, news events, Wikipedia, internet health, weather alerts, night lights. Plus SHTF-specific: radiation, energy grid, water, emergency activity.

---

## Sources conversation 2: tiering and practical constraints

**Peter**: More on satellites beyond fires, if possible. Internet health includes major websites/services? GTFS-RT for city transit? Include SHTF-relevant sources: radiation, energy, water, emergency activity.

**Claude**: 

**Satellite, beyond fires**: open constellations (Sentinel, MODIS, GOES) provide pre-computed products — night lights (power outages, blackouts), atmospheric trace gases (NO₂, SO₂, CO, methane), aerosols/smoke, soil moisture/drought, snow/ice/flood extent, sea surface temperature, vegetation health (NDVI). Bandwidth is manageable: ingest regional aggregates/anomaly products, pull imagery tiles only on-demand after alerts.

**Internet health — both layers**:
- Infrastructure: BGP routing, country/region traffic volumes, DNS, outage detection
- Service layer: radar-style per-service traffic trends, provider status pages, CT logs
- Plus: own active prober on VPS (HTTP/DNS checks against major services every few min)

**GTFS-RT**: yes, I was too pessimistic. One parser handles every agency; marginal cost per city is registration. Compute one number per city per bin: active vehicles vs seasonal norm. Fifty cities across continents is achievable. Transit stopping is a high-threshold event (strikes, disasters, fuel shortages, evacuations).

**SHTF sources**:
- **Radiation**: government networks (EU exchange platform, national equivalents) + citizen sensor networks (Safecast-style). Gamma dose rates, near-real-time, geocoded. The star of the category.
- **Energy**: European grid transparency (load, generation, cross-border flows, prices, frequency) is exceptional. Gas flows too. Grid frequency deviations are sensitive real-time stress signals. US coarser.
- **Water**: river gauges, reservoir levels (US/EU hydrological agencies), satellite altimetry. Municipal drinking-water status is NOT published anywhere real-time — known blind spot; proxies are reservoirs, drought indices, emergency alerts.
- **Emergency activity**: CAP-format alert feeds (official, many countries), GDACS disaster feeds, emergency squawk codes in ADS-B data, public-safety radio activity (channel busyness vs normal).

Updated Tier 1 (SHTF-weighted): seismic, markets, news, Wikipedia, internet health (both), weather, radiation, night lights. Immediate Tier 2: ADS-B, energy grid, GTFS-RT, trace gases, fires, air quality, CAP, search trends, conflict/protest databases, water, more satellite products, public-safety radio.

---

## Architecture conversation 1: processes, storage, UI

**Peter**: What will the architecture of the system look like? A single process? Will data be stored locally on a continuously running server? What would be a good user interface? What about account registration for services needing logins, and the system architecture itself?

**Claude**: 

**Account registration**: I can't do it (CAPTCHAs, email verification, ToS violations). Operator registers accounts personally. Most Tier 1/2 sources need no account; those that do are legitimate to register in the operator's name or org identity.

**Architecture — not single-process but loosely coupled around one database:**
1. **Pollers**: async, per-source isolation (one broken API never stalls others), retry/backoff, health-instrumented
2. **Consolidator**: idempotent fold into cascade every few minutes
3. **Detector**: per consolidation cycle (fine scales) / hourly+ (coarse scales); writes alerts
4. **API/UI server**: read-only dashboard + push notifications
All communicate through the database; systemd supervises; SQLite sufficient (WAL mode).

**Storage**: local on the VPS, compact. Consolidated store is thousands of series × ~180 bins = MB to low GB. Keep the surprise archive forever; export daily Parquet to local machine for modeling.

**UI — three layers:**
1. **Push alerts (primary)**: severity, region, one-liner, link. This is the interface for an early-warning system.
2. **Web dashboard**: map colored by current surprise, alert feed, click any cell to see timeline of each stream vs its predictive band (the geometric bins map naturally onto a zoomable timeline).
3. **Alert explanation**: LLM assembles anomalies + recent news into a hypothesis narrative.

Deployment split: **VPS for 24/7 ingestion** (pollers, consolidator, Layer 0, detector, storage, API), **local machine for modeling** (Layer 1 fits, backtests, syncs archive in MB/day). Degrade to "last-known picture + silence map" if feeds die.

---

## Schema and consolidation

**Peter**: The data representation should have multiple time scales: faster for recent events, longer bins for older events. Fixed ratio between bin width and distance in time. Think about internet bandwidth. Local or VPS? Sampling strategy?

**Claude**: 

**Multi-scale time bins**: "fixed ratio between bin width and distance in time" is exactly right — geometric bins, bin width w ∝ age. ~8 bins per doubling gives constant bins per octave. Round-robin databases have used this for decades (RRDtool, Whisper).

Implementation: consolidation cascade with ring buffers. Raw/fine data (minute resolution) for 48 hours, then aggregate into coarser rings (5-min for a week, hourly for a month, daily for two years, weekly beyond). **Crucially, keep min/max/count/variance per bin, not just mean** — otherwise anomalies vanish in coarse scales.

**Bandwidth**: much smaller than feared if you poll aggregated endpoints, never raw firehoses. 1–3 GB/day down; use compression, conditional fetches. Retained data (the compact store): megabytes/year.

**Local vs VPS split**:
- **VPS wants 24/7 ingestion**: stable IP, uninterrupted availability. Small $5–10/month instance handles everything.
- **Local wants the modeling compute**: GPU/CPU cheap, iteration fast. Syncs the consolidated store (MB/day).

**Sampling economics**: don't solve the full expected-information-gain optimization for v1. Discretize into three cadence tiers (baseline/elevated/focus). Recent surprise in a region → elevated. Unresolved alert nearby or user zooms in → focus. Demotion by decay. Geographic principle: **many cities sampled coarsely beat one city finely**; cross-regional agreement is where corroboration power lives.

---

## Topical focus and the coupling graph

**Peter**: Focus should not only be geographic but also by topic — the kind of thing being measured.

**Claude**: Perfect. Attention should propagate along the coupling graph, and geography and topic are the two kinds of edges it has.

Before Layer 1 learns anything, human-assigned topic tags on each source serve as prior edges; as the coupling model fits, learned edges take over. The allocator needs no new machinery: **promotion pressure just diffuses from surprising nodes along graph edges, spatial and topical alike**, with a coverage floor underneath as a guard.

So a radiation anomaly propagates:
- Spatially: boost sources in and around that region
- Topically: boost all radiation sources globally (the prior tag), PLUS whatever Layer 1 has learned correlates with radiation (grid data, evacuations, regional attention)
- User interaction: temporary focus tier on a chosen region or topic
- All governed by expected information gain: cost per poll vs benefit (uncertainty reduction in the nearby region/topic)

---

## Full converged design — the specification

**Claude** (after all above discussion): The architecture has converged:

Sources → config-driven pollers → typed Layer 0 fleet (online Bayesian, PIT-audited, nursery-gated) → surprise-field archive (geometric bins, quantile sketches, presence channel) → sparse Layer 1 coupling model → alert logic (persistence + coherence + corroboration) → LLM interpreter → map/timeline UI + push alerts — all governed by a three-tier attention allocator (baseline/elevated/focus) with geographic+topical diffusion and a coverage floor.

Produced the full specification document: `worldwatch-architecture-v0.1.md` (16 sections, from purpose through non-goals).

---

## P0 implementation plan

**Claude** (in response to request to move to Claude Code):

Created three additional documents:

1. **CLAUDE.md**: project guide for Claude Code sessions. Glossary, tech stack (Python + async + SQLite + systemd + FastAPI), repo layout conventions, coding guardrails (the interchange contract is sacred; per-source isolation; no imputation; calibration-as-tests).

2. **p0-implementation-plan.md**: concrete first milestone. Definition of Done (14-day unattended run, seismic event flagged, push alerts working). The 8 Tier-1 sources. Build order where every step leaves a working system. Full SQLite DDL schema. Seven key algorithms with acceptance criteria (octave binning, t-digest merge, Student-t SSM, NB count model, presence model, rolling PIT audit, naive alert engine). Explicitly deferred to P1+ (Layer 1 GGM, corroboration proper, prober, allocator, GPD tails, LLM, Parquet export). First opening tasks.

3. Plus `worldwatch-architecture-v0.1.md` and `global-anomaly-sources.md` also saved.

---

## Key design decisions (summary)

| Decision | Rationale | Consequences |
|----------|-----------|--------------|
| Hierarchical predictive coding (Layer 0 → Layer 1 → LLM) | Modular, reduces capacity waste, makes components swappable | Interchange currency (q_values) must be exactly right |
| Online Kalman-style fits for Layer 0, no training runs | Free computation, works from day one, robust to drift | Constrains model family but that's OK |
| Quantile (PIT) as interchange currency, never z-scores | Honest under heavy tails, composes across sources, calibration-testable | Need to keep quantile sketches, not moments |
| Presence channel as a modeled stream | Missing data is signal, self-instrumenting | Requires two-channel (value + presence) per source |
| Sparse Layer 1 graphical model | Parameters are the product (coupling graph); readable; modest compute | Traded deep-learning flexibility for interpretability |
| Three-tier attention allocator with graph diffusion | Efficiently uses API budgets, geographic+topical focus, self-aware blindness | Simpler (tier rules) for v0; EIG bandit for v1 |
| SQLite + systemd, not Kafka/Postgres/k8s | Boring, maintainable, self-hostable, operator preferences | Scales until it doesn't (which is fine for Phase 0+) |
| LLM only at the top, event-driven | Avoids cost/calibration problems; explains without influencing | Separate concerns, simpler pipeline |
| Push alerts as primary UI | Systems like this aren't checked; they find you | Dashboard is secondary (still important for investigation) |

---

## Open questions & parameters (intentionally deferred)

From the spec, section 15:
- bins-per-octave (default 8)
- fine-window length
- H3 resolution per scale
- nursery pass criteria (PIT uniformity threshold)
- persistence N per scale
- corroboration threshold (how many modalities required)
- tier multipliers (baseline vs elevated vs focus cadence)
- coverage-floor cadence
- GPD threshold quantile
- refit cadences (Layer 0, Layer 1)

These emerge at build time from the infrastructure (API rates, VPS bandwidth, local compute) and live tuning against real alerts.

---

## Why this design is interesting

The system isn't just an alert-generating tool; it produces three scientific outputs:

1. **The coupling graph**: an empirical map of how world systems couple, which streams predict which, at what lags and scales. No one has published this across physical + informational + economic streams.

2. **Event fingerprints**: cluster historical alerts in signature space and you get an empirical taxonomy of events. New events are classified by nearest fingerprint. Reveals unnamed event clusters.

3. **Lead-time science**: because evaluation metric is "how long before mainstream coverage," you accumulate data on which sources are early for which event types. Quantifies that official news reacts slowly.

Secondary (but scientifically primary): **the protocol itself** — the calibrated-surprise interchange format. Others could implement it, turning one system into infrastructure for epistemic verification (splices with Peter's verification work).

---

## Phasing (from the spec)

- **P0** (weeks): 8 Tier-1 sources → pollers → cascade → Layer 0 → surprise archive → map + push alerts. Naive corroboration (count of modalities over threshold). Runs unattended.
- **P1**: Layer 1 GGM + explained-away logic + corroboration proper; backtest harness; nursery/PIT auto-promotion; presence+prober; Tier-2 sources.
- **P2**: allocator tiers + graph diffusion; LLM interpreter; fingerprint clustering.
- **P3**: deep Layer 1 v2 (behind measured lead-time test); LLM-as-sensor; EIG bandit allocator; protocol publication.

---

## Artifacts created

Four files in `/mnt/user-data/outputs/`:
- `CLAUDE.md` — project guide (read first in Claude Code sessions)
- `worldwatch-architecture-v0.1.md` — the authoritative spec
- `p0-implementation-plan.md` — build order, schema, algorithms, acceptance criteria
- `global-anomaly-sources.md` — data source catalog

All to be saved in repo `docs/` (except CLAUDE.md in repo root) and version controlled.

---

## Next steps in the project

1. **Set up repo** with the directory layout from CLAUDE.md.
2. **Endpoint research** for Tier-1 sources (done in Claude Code; test URLs live).
3. **Operator registrations** for sources requiring auth (personal responsibility; keys via env vars).
4. **Build order** from p0-implementation-plan.md, starting with schema + octave-bin math.
5. **First two pollers** (seismic, Wikipedia) to establish the pattern.
6. **Cascade + Layer 0 (continuous)** → wired to markets, radiation.
7. **Layer 0 (count)** → seismic counts, news, Wikipedia, weather.
8. **Remaining pollers** → presence channel → naive alerting → push + map.
9. **14-day soak** with real event flagging (seismic guarantees this).

---

## Design philosophy summarized

Keep it simple at the bottom. The world is complex; the job of the foundation is to be transparent and trustworthy, not to be smart. A model that fails in a way you don't understand is worse than a simple model that fails in a way you do. Fanciness strictly increases with height, because you have more data (the surprise field is all of Layer 0's outputs) and you know what you're explaining (the coupling structure). Calibration is non-negotiable because the upstream outputs feed into combinatorial logic (corroboration); if a q_value doesn't mean what it claims, alerts cascade or disappear silently. Self-instrument everything; the system watching itself prevents a whole class of failure modes (blindness from attention misallocation).

The protocol is the product. Once the calibrated-surprise interchange is locked down and working, the system could be swapped (better Layer 1, different alert logic, different sources) without breaking the contract. That's where the infrastructure value sits.

---

**End of conversation archive. Salted: 2026-07-09.**
