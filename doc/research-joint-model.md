# Research direction — from calibrated surprise to a joint world model

Status: **proposal** (2026-09-29). Nothing here is implemented, and nothing
here changes P0. Each amendment below becomes an ADR if adopted. Linked from
ROADMAP § "Layer 1 — after P0". Working name for the joint model:
`mappamundi` (§15).

Read with: `worldwatch-architecture-v0.1.md` (this note would amend §2, §5,
§7, §13 and §14), `research-tails-and-dependence.md` (extremes; this note
defers to it there), `research-changepoint-replay.md` (replay method), and
ADRs 0001, 0003 and 0004.

## Summary

Worldwatch models each stream alone and treats agreement between modalities
as evidence. That quietly assumes the modalities are independent in normal
times. This note proposes growing Layer 1 into a joint model of all streams:
a sparse graph, changing over time, that says which streams move together,
which lead which, in which regime, and, where the data allow it, which cause
which.

Its outputs go beyond surprise: a forecast for every stream, surprise *given
the rest of the world*, a measure of how tightly coupled the world is now,
and conditional claims written before their data arrive and scored after.
The note sets out what the surprise field cannot show, the design changes
that fix it, a ladder of models from a static sparse graph up to causal
identification, the published results that make each rung workable, and a
test each rung must pass before it ships.

## 1. Why

Market commentary is full of signed, conditional claims: "weak jobs data is
good for stocks, because the Fed will cut". Such a claim is a causal chain
through expectations. Its fault is not that it is wrong. Its fault is that
the commentator picks the sign after the market has moved, so the claim can
never fail. And the sign does depend on the state of the world: rising US
unemployment has tended to lift stocks in expansions and hurt them in
contractions (Boyd, Hu & Jagannathan 2005).

A quantitative version would state the chain, its sign and its dependence on
the regime *before* the data arrive, with calibrated uncertainty. It would
also cover physical, infrastructural, attention and economic systems
together, not markets alone. Worldwatch already has the hard part: calibrated,
unit-free surprise for every stream, place and scale. It lacks the joint
model.

## 2. What the current design already is

**Marginals plus copula.** Layer 0 fits one dynamic model per stream and
emits the PIT; Layer 1 models the dependence between PITs. This is the
standard two-stage split of copula modelling (Joe 2014), used for economic
time series as conditional copulas (Patton 2006, 2012). A Gaussian graphical
model on z = Φ⁻¹(q) is a Gaussian copula: the *nonparanormal* of Liu,
Lafferty & Wasserman (2009). Calibration (P6) makes each z standard normal,
so it does double duty: it validates alerts and it validates the copula.
ADR 0003 already made `q_value` the raw randomized PIT for discrete streams,
which is the right input for Layer 1. For every stream `q_value` is the
signed raw PIT (q < 0.5 low, q > 0.5 high), so every z-value, and every edge
estimated from them, carries direction.

**Corroboration is a test for suspicious coincidences.** Barlow (1989)
argued that a sensory system should notice joint events that happen more
often than the product of their parts predicts. The P0 rule (≥2 independent
modalities) uses independence as its baseline: two rare events together
count as very rare. Layer 1 replaces that baseline with the learned normal
coupling. A storm makes weather, outage and news surprises occur together;
a joint model knows this and does not count three confirmations. Everything
in this note follows from that one substitution.

## 3. What a surprise-only field cannot show

1. **Shared slow drivers.** Each Layer-0 model tracks its own level and
   trend. A slow driver common to many streams (a recession, a season-long
   heatwave, a slow rise in global news volume) is absorbed separately by
   every stream's trend and never reaches the residuals. At slow timescales
   the structure between streams lives in the Layer-0 *states*, not in their
   errors. The coarse octaves do not help: they judge coarse bins against
   predictives whose trend has already adapted.
2. **Coupling that depends on the state.** "Bad news is good news" is an
   edge whose sign flips with a regime, and a regime is a level: policy
   stance, market stress, season. q-values strip levels out by design.
3. **Direction and lag.** The spec promises "which streams predict which, at
   what lags and scales" (§1, output 2). A same-time graphical model gives
   neither direction nor lag.
4. **Joint extremes.** A Gaussian copula has zero tail dependence; see
   `research-tails-and-dependence.md`. The Gaussian formulas in this note
   hold in the bulk only.

## 4. Proposed amendments to the principles

**P2′ — two currencies go up, both unit-free.** Besides `q_value` (surprise
of the innovation; fast), Layer 0 also emits `q_level`: the PIT of the
stream's current smoothed level under that stream's own long-run reference
distribution (by season, where the stream has seasons). The cascade's
t-digest sketches can hold the reference cheaply. `q_level` is a percentile,
not a value in source units, so the spirit of guardrail 1 survives: no raw
values reach Layer 1. Its wording changes from a field list to "unit-free
tail probabilities only". Climate science calls this a standardized anomaly
against climatology; here it is calibrated. It carries the slow drivers of
§3.1 and the regime covariates of §3.2. A stream gets `q_level` only once its
reference history is long enough (a nursery for levels).

**P2″ — predictions come down, but only as Layer-1 outputs.** In predictive
coding the upper level sends its prediction down and the lower level passes
up what the prediction missed (Rao & Ballard 1999). Worldwatch should
compute that residual, surprise given the rest of the world, as a Layer-1
output (`q_cond`, `q_fcst`; §6). It should not feed Layer-1 predictions back
into Layer 0's update. The feedback version would make the surprise archive
depend on Layer 1, while Layer 1 is fitted on the archive: a circle. Layer 0
stays bottom-up, and its q-values keep their frozen meaning.

**P7′ — one model family, fitted in levels.** "ONE Layer-1 model" becomes one
family whose levels nest (§5). Production runs the simplest level that has
passed its acceptance test (§12). P4 still holds: complexity rises with
height.

## 5. The model ladder

Each level adds one thing to the level below.

| Level | Adds | Main method | Key output |
|---|---|---|---|
| L1.0 | a static sparse graph of the bulk | graphical lasso on z, rank-based | coupling graph with error rates; `q_cond`; `q_joint` |
| L1.1 | a few hidden common drivers | sparse-plus-low-rank precision | named drivers; a graph cleaned of them |
| L1.2 | change over time and by regime | kernel-weighted, fused and joint graphical lasso; covariate-dependent graphs | regime state; correlation breaks; coupling over time |
| L1.3 | lag and direction | graphical VAR; PCMCI+ | lead–lag map; `q_fcst`; lead time per source |
| L1.4 | joint extremes | Hüsler–Reiss graphical models | joint surprise valid in the tails |
| L1.5 | causal claims, where identifiable | context variables, invariance, natural experiments | edges labelled with how they were identified |
| v2 | nonlinear, learned | deep sequence model (spec §7) | better forecasts, if it beats L1.x on lead time |

### L1.0 — a static sparse graph

- **Methods.** The graphical lasso (Friedman, Hastie & Tibshirani 2008), or
  neighbourhood selection (Meinshausen & Bühlmann 2006), which runs one lasso
  per stream in parallel. Under incoherence conditions both recover the true
  graph from about d² log p samples, where p is the number of streams and d
  the largest number of neighbours any stream has (Ravikumar et al. 2011).
  More streams cost only a logarithm; hubs cost a square.
- **Scale.** At a given penalty, the graphical lasso splits exactly along the
  connected components of the thresholded sample covariance (Witten,
  Friedman & Simon 2011; Mazumder & Hastie 2012). Geography makes thousands
  of streams fall apart into many small problems.
- **Robustness.** Estimate each correlation from Kendall's τ, as
  r = sin(πτ/2), rather than from z (Liu et al. 2012; Xue & Zou 2012). The
  rank estimate tolerates a marginal model that drifts before the PIT audit
  notices, and the extra noise of randomized discrete PITs.
- **Priors.** Spatial adjacency and topic tags (spec §7) enter as weights on
  the penalty, with a smaller penalty on a prior edge. They are not a hard
  mask, so the data can overrule them. Co-located sensors of one network
  start near full dependence ("Scarce data" in the tails note).
- **Error rates.** The graph is a published output, so every edge needs an
  error rate. Stability selection (Meinshausen & Bühlmann 2010) bounds the
  expected number of false edges; the de-sparsified graphical lasso (Janková
  & van de Geer 2015) gives each edge a confidence interval.
- **Baseline.** The Chow–Liu tree (Chow & Liu 1968), the maximum spanning
  tree on mutual information, is the maximum-likelihood tree. Every spanning
  tree has the same number of parameters, so it is also the best tree by
  MDL. Tan, Anandkumar & Willsky (2011) extend it to forests with consistent
  pruning. It fits in seconds, and every richer level must beat it.
- **Presence nodes.** `presence_q` streams are nodes like any other (P5). A
  cable cut couples silent probes with a drop in traffic; a region where
  many sources fall silent shows up as a dense patch of the presence
  subgraph.

### L1.1 — hidden common drivers

A few global drivers, such as the daily cycle, risk-on/risk-off and the
volume of the news cycle, would otherwise appear as a dense mass of weak,
spurious edges. Chandrasekaran, Parrilo & Willsky (2012) write the precision
of the observed streams as a sparse part minus a low-rank part, Θ = S − L,
and fit it by convex optimization, with conditions under which the two parts
are identifiable. S is the coupling graph proper; L holds the drivers.

This makes the spec's "graphical / factor model" precise, and it gives "one
storm ≠ three anomalies" a name: the storm is a factor, and the three streams
are independent given it. Joint surprise under any correlated model already
avoids triple counting; the factor adds the explanation.

For the slow drivers of §3.1 the same idea applies to `q_level`: a dynamic
factor model over the level anomalies (Stock & Watson 2002), with common
trends in the sense of Stock & Watson (1988). These factors are the slow,
named state of the world, its climate, where the innovation graph is its
weather.

### L1.2 — change over time and by regime

- **Smooth change.** A kernel-weighted covariance fed to the graphical lasso
  tracks a graph that changes smoothly, with proven rates (Zhou, Lafferty &
  Wasserman 2010). An exponentially weighted covariance is the online special
  case and matches Layer 0's forgetting.
- **Abrupt change.** The time-varying graphical lasso (Hallac et al. 2017)
  penalizes differences between consecutive precision matrices; an L1
  penalty on the differences gives graphs that stay fixed between breaks.
  Bayesian online change-point detection (Adams & MacKay 2007), already tried
  on counts in `research-changepoint-replay.md`, can run on joint surprise to
  decide when to refit.
- **Related graphs.** The joint graphical lasso (Danaher, Wang & Witten 2014)
  fits several graphs that share structure: one per region, per regime, or
  per octave. The octave case matters because coarse scales have few bins;
  sharing lets them borrow strength from the fine scales.
- **Edges that depend on the regime.** Let the precision depend on a short
  covariate vector c built from the `q_level` of chosen streams: market
  stress, season, and policy stance once macro sources exist (§7). Kolar,
  Parikh & Xing (2010) estimate such covariate-dependent graphs by kernel
  smoothing in covariate space. A finite set of regimes, each with its own
  Θ_k and tied together by the joint graphical lasso, is the discrete
  version. "Bad news is good news" is then an edge Θ_ij(c) whose sign changes
  with c, and the model states it before the next release, not after.
- **Change the world did not make.** q-values are frozen under
  `model_version` (spec §5), so a Layer-0 refit changes the data without any
  change in the world. Version changes go into the contexts table (§7), so
  the change-point machinery does not mistake them for regime shifts.

### L1.3 — lag and direction

- **Graphical VAR.** A sparse vector autoregression on z, with a sparse
  precision on its innovations: directed edges for lagged effects and
  undirected edges for same-bin coupling. This is the time-series chain graph
  of Dahlhaus & Eichler (2003); Basu & Michailidis (2015) give estimation
  theory for sparse VARs under temporal dependence. Its one-step forecast
  gives `q_fcst` (§6).
- **The PCMCI family.** PCMCI (Runge et al. 2019), PCMCI+ for same-time
  links (Runge 2020) and LPCMCI for hidden confounders (Gerhardus & Runge
  2020) were built for autocorrelated, heterogeneous Earth-system series,
  which is worldwatch's case. Implementation: `tigramite`.
- **Direction of same-time effects.** When the noise is non-Gaussian, linear
  models can identify the direction of same-time effects (LiNGAM, Shimizu et
  al. 2006; VAR-LiNGAM, Hyvärinen et al. 2010). Worldwatch's heavy tails help
  here, but Φ⁻¹ removes exactly the non-Gaussianity these methods need. Such
  fits should work on the Student-t scale, e = t_ν⁻¹(q). The shape is set
  per observation, not per model version: a continuous stream's predictive
  has ν_t = min(n_t, obs_dof) (`layer0/continuous.py`), wider while evidence
  on the noise scale is thin. The inversion e_t = t_{ν_t}⁻¹(q_t) therefore
  needs ν_t stored with each record (§8).
- **A caution.** Continuous-optimization DAG learners (NOTEARS, Zheng et al.
  2018; DYNOTEARS, Pamfil et al. 2020; DAGMA, Bello, Aragam & Ravikumar 2022)
  owe much of their benchmark success to the ordering of variances in
  simulated data (Reisach, Seiler & Weichwald 2021). On standardized data
  like the surprise field that advantage disappears. Use them, if at all,
  only as a comparison against PCMCI+ on the same data.
- **Lead-time science falls out.** Under ADR 0001 news is context, never
  evidence. The GDELT node stays in the joint model as a *target*: lagged
  edges from sensors into news, with their lags, measure spec §1 output 4
  ("which sources are early for which event types") instead of asserting it.

### L1.4 — joint extremes

This level is the subject of `research-tails-and-dependence.md`:
tail-dependence coefficients, the extremal coefficient θ, and Hüsler–Reiss
graphical models (Engelke & Hitz 2020) with their structure learning. This
note only fixes the division of labour. The Gaussian levels describe the
bulk and supply the coupling graph for the allocator and for interpretation;
the extremal model scores joint tails for alerts. Where the two graphs
disagree, with streams coupled only in the extremes or only in the bulk, the
disagreement is itself a finding.

### L1.5 — causal identification

A learned edge is associational until something identifies it. Worldwatch
has unusually good material for identification:

- **Context variables.** Joint Causal Inference (Mooij, Magliacane & Claassen
  2020) adds context variables as exogenous nodes and learns from all
  contexts pooled: a scheduled release, an M6+ quake in a cell, a Layer-0
  version change, an allocator tier change.
- **Natural experiments.** A scheduled release has a known time. In a tight
  window after it almost nothing else happens, so a move inside the window
  is the release's doing; this is how the event-study literature on monetary
  policy identifies effects (Gürkaynak, Sack & Swanson 2005). Earthquakes,
  eruptions and storms do not read the news or respond to prices, which makes
  them clean sources of variation for how shocks spread into infrastructure,
  attention and markets.
- **Invariance.** A stream's causal parents are those whose relation to it
  stays the same across environments (Peters, Bühlmann & Meinshausen 2016).
  Worldwatch has environments everywhere: regions, seasons, regimes.
- **Shifts reveal direction.** CD-NOD (Huang et al. 2020) uses changes in
  distribution to orient edges, and the sparse mechanism shift hypothesis
  (Schölkopf et al. 2021) holds that a change of regime alters few
  mechanisms. Nonstationarity, usually the enemy of structure learning,
  becomes the signal.

Every edge in the published graph carries a type (associational, predictive,
or identified) and, for an identified edge, the route that identified it.

### v2 — the deep model

Unchanged from spec §7. Neural graph learners, such as neural relational
inference (Kipf et al. 2018), amortized causal discovery (Löwe et al. 2022)
and the learned adjacency of spatiotemporal graph networks, find whatever
links help prediction, with no guarantee that those links are the structure.
v2 has to beat the best L1.x on lead time, and the L1.x graph stays as the
explanation layer.

## 6. Outputs beyond surprise

Formulas at the Gaussian-copula levels (L1.0–L1.3), with z = Φ⁻¹(q),
correlation matrix Σ and precision Θ = Σ⁻¹ (implied by S − L when L1.1 is on).

**Surprise given the world.** For stream i, given every other stream in the
same bin:

```
z_i | z_−i  ~  N(μ_i, 1/Θ_ii),    μ_i = −(1/Θ_ii) · Σ_{j≠i} Θ_ij z_j
q_cond,i   =  Φ( (z_i − μ_i) · √Θ_ii )
```

If some neighbours are missing, condition on the observed set O alone:
μ_i = Σ_iO Σ_OO⁻¹ z_O, with variance Σ_ii − Σ_iO Σ_OO⁻¹ Σ_Oi. That is
marginalizing, not imputing, so P5 holds.

**Forecast surprise.** `q_fcst` is the same PIT under the L1.3 one-step
forecast, which uses past bins only. `q_cond` explains; `q_fcst` predicts,
and only `q_fcst` is honest evidence of lead time.

**Four cases.** Reading `q_value` against `q_cond` sorts every anomaly:

| `q_value` | `q_cond` | Reading |
|---|---|---|
| normal | normal | nothing |
| extreme | normal | explained away: the neighbours predicted it (one storm; rain on a dose-rate probe) |
| normal | extreme | correlation break: the neighbours moved and this stream did not, or it moved against them |
| extreme | extreme | an anomaly the rest of the world does not account for |

The spec's explained-away discount and correlation-break score (§7) are the
second and third rows.

**Joint surprise of a region.** For the streams S active in one region and
bin:

```
D² = z_Sᵀ (Σ_SS)⁻¹ z_S  ~  χ²(|S|)    ⇒    q_joint = 1 − F_χ²(D²; |S|)
```

So `q_joint` is calibrated, and auditable like any q-value. Tail decisions
use L1.4.

**Coupling index.** The total correlation of the Gaussian copula
(multi-information; Watanabe 1960) is

```
TC = −½ · log det Σ
```

the number of nats by which the world's streams fail to be independent.
Tracked through L1.2, it measures how coupled the world is now. In finance,
Kritzman et al. (2011) found that a rising share of variance carried by a few
factors tended to come before market drawdowns. Whether coupling across
modalities behaves the same way is an open question that worldwatch is
placed to answer.

**Named drivers.** The L1.1 factor scores over time, each with the streams
that load on it.

**Regime state.** L1.2's posterior over regimes, or its covariate vector c,
together with the edges that change across it.

## 7. New sources and contexts

- **Precipitation.** ADR 0001 names rain as the known confounder of
  radiation: rain washes radon decay products to the ground and raises gamma
  dose rates regionally. An open precipitation source (Open-Meteo is one
  candidate; its terms need checking) makes this the first real test of
  explaining away (§12).
- **Macro releases, as an external-forecast flavour of Layer 0.** For a
  scheduled release, the predictive distribution is a published forecast plus
  a learned spread, not a Layer-0 fit. The PIT and its audit apply unchanged,
  so a release enters the surprise field like any sensor, and "weaker than
  expected" becomes a calibrated q. Sources: ALFRED (the St. Louis Fed's
  archive of data as first published, which is the point-in-time record), the
  BLS API, and, as public forecasts, the Survey of Professional Forecasters
  and the regional Fed nowcasts. Polled consensus figures are mostly
  proprietary, and that is the main gap. FX remains the open market gap
  (ROADMAP).
- **Contexts table.** Exogenous events with known times: the release
  calendar, Layer-0 version changes, allocator tier changes (P9 already logs
  these), and worldwatch's own outages. These are the context nodes of L1.5.
- **The allocator loop.** The allocator raises cadence along coupling edges
  (spec §9); a higher cadence changes the precision of the neighbours' data;
  the data then refit the graph. Unless the allocator log enters as a
  context, the graph can end up confirming itself.

## 8. Contract and storage changes

Each change would be an ADR amending spec §5 (numbers from 0005 on).

| Change | Why | Cost |
|---|---|---|
| `q_level` per (stream, cell, scale, bin) | slow drivers and regimes (§3.1–3.2) | one column; a long-run reference per stream |
| noise shape ν_t per record, for continuous observations | Student-t scale for direction tests (L1.3); ν_t = min(n_t, obs_dof) changes with every observation (`layer0/continuous.py`), so a value per model version cannot invert q | one column, continuous streams only; Layer 0 computes ν_t at each update |
| both ends of the PIT interval, F(y−1) and F(y), for discrete observations | exact rank-likelihood copula fits (Hoff 2007); `q_value` and `q_detect` do not pin the interval | two columns, count streams only; no new computation: the count model already computes both ends at each update and passes them to `conservative_q` (`layer0/count.py`) |
| `layer1_outputs` table: `q_cond`, `q_fcst`, `q_joint`, factor scores, regime, `layer1_version` | §6 | new table; never read by Layer 0 |
| `contexts` table | L1.5 and the allocator loop (§7) | new table |
| versioned graph file: edge, weight, interval, stability, lag, type, identification route | publishing spec §1 output 2 | one file per fit; content-addressed snapshots fit the thin storage interface in CLAUDE.md |

The randomized PIT stays the calibration currency (ADR 0003), `q_detect`
stays the detection currency, and both tails stay in the archive (ADR 0004),
which the signed edges of this note need.

## 9. Constraints that shape the fit

- **Samples.** Recovery needs about d² log p samples per graph, and forgetting
  shrinks the effective sample. Fine octaves have plenty; coarse octaves
  borrow from them through the joint graphical lasso.
- **Compute.** Fits run on the local machine (spec §11). Screening splits the
  problem, and neighbourhood selection runs one lasso per stream in parallel.
  Scoring on the VPS is sparse linear algebra.
- **As-observed data.** APIs revise their past: USGS revises magnitudes, and
  NWS upgrades or cancels alerts. A replay built from today's API history
  therefore uses information worldwatch did not have at the time. The
  surprise archive is as-observed by construction. For the sources Layer 1
  will use, the cold archive should keep as-observed raw data from now on.
  The clean record starts when the VPS went live.

## 10. Claims ledger

This is the step from monitor to model: a claim that can fail.

- A claim is a conditional prediction written before its data: "in regime c,
  a release surprise q in X implies this distribution for Y over the next h".
  The model writes one for every scheduled release. A person can write one
  too, including a pundit's narrative restated in this form.
- Proper scoring rules score each claim: log score and CRPS (Gneiting &
  Raftery 2007). Layer 1's conditional predictives get the same PIT audit as
  Layer 0 (P6, one level up).
- To prove that a claim came before its outcome, hash each day's ledger and
  publish the hash through a timestamping service or a chain transaction,
  which costs next to nothing. The content hash proves what a claim said;
  the published timestamp proves when.

## 11. Fingerprints as a concept lattice

Spec §1 output 3 is an empirical taxonomy of event signatures, planned as
clustering. But an episode's signature is naturally binary (which streams,
at which scales, in which order and direction), and one kind of event shares
attributes with several others: an outage caused by a storm is both. A
taxonomy with multiple inheritance is a concept lattice (Ganter & Wille
1999), and MDL (Grünwald 2007) decides which concepts pay for themselves.

An MDL-selected concept DAG over episodes × attributes is the discrete
counterpart of the L1.1 factors: factors are continuous hidden causes,
concepts are discrete event types. The sparse-coding route in the tails note
(Földiák 1990) sits between the two.

## 12. What each level must show before it ships

| Level | Acceptance test |
|---|---|
| L1.0 | held-out log-likelihood beats independence (an estimate of TC, with an interval); edges stable under resampling; beats Chow–Liu; **rain → radiation**: with `q_cond`, single-source radiation alerts during rain fall at fixed sensitivity |
| L1.1 | fewer edges at equal held-out likelihood; drivers a person can name |
| L1.2 | correlation breaks flag known decouplings; version changes absorbed by contexts, not found as regime shifts |
| L1.3 | recovers known physics with lags (quake in a cell → outage there → news); lead time per source; `q_fcst` calibrated |
| L1.4 | per the tails note: fewer false alerts in the tails than the Gaussian copula at fixed sensitivity |
| L1.5 | identified edges invariant across environments; release-window effects with intervals |
| ledger | Layer-1 predictive PITs uniform; log score beats the unconditional model |

## 13. Phasing (proposed amendment to spec §14)

- **P1** (once P0's definition of done is met): L1.0 and L1.1; `q_cond` and
  `q_joint`; the contexts table; a precipitation source; tails research in
  the order its note proposes.
- **P1.5:** L1.3 and `q_fcst`; lead-time science.
- **P2:** `q_level` and L1.2 regimes; macro releases through the
  external-forecast flavour; L1.4.
- **P3:** L1.5; the public graph with edge types; the claims ledger; v2
  behind its acceptance test.

## 14. Open questions

- Can `q_level` be audited for calibration when it is this autocorrelated?
  Probably only over long windows, pooled across similar streams.
- One Layer-1 model or two? The tails note proposes Layer 1 as an extremal
  graphical model; this note keeps a bulk model and an extremal model and
  reads their differences.
- How few bins can a coarse octave have before shared structure does more
  harm than good?
- Which covariates earn a place in c, and who picks them: a person, or the
  L1.1 factors?
- Does the allocator loop (§7) need more than a context variable, for
  example fitting only on data taken at baseline cadence?
- Should guardrail 1 gain a split like guardrail 8's? Online detection
  would still see only the contract. Offline modelling on the local machine
  could read the as-observed raw data of §9, and its improvements would reach
  Layer 0 only as versioned model releases (`model_version`), never as a
  live path from raw values into Layer 1.

## 15. Name

"Worldmodel" is accurate and forgettable. Working name for the joint model:
**`mappamundi`**. Medieval world maps put surveyed coasts beside legend and
sea monsters; this one keeps the coasts and replaces the monsters with
calibrated edges. Plainer alternatives: `worldgraph`, `wholeworld`. As of
2026-09-29 all three, and `worldmodel`, are free on PyPI; `worldview`,
`orrery`, `umwelt` and `terrella` are taken.

Suggestion: keep the repository name, since worldwatch is the observatory,
and use the new name for the model package. Rename the whole project only if
the model becomes the main product.

## References

Written from memory; check volume and page details before citing outside
this repository. Extremes references are in `research-tails-and-dependence.md`.

- Adams, R. P. & MacKay, D. J. C. (2007). Bayesian online changepoint
  detection. arXiv:0710.3742.
- Barlow, H. B. (1989). Unsupervised learning. *Neural Computation* 1(3),
  295–311.
- Basu, S. & Michailidis, G. (2015). Regularized estimation in sparse
  high-dimensional time series models. *Annals of Statistics* 43(4),
  1535–1567.
- Bello, K., Aragam, B. & Ravikumar, P. (2022). DAGMA: learning DAGs via
  M-matrices and a log-determinant acyclicity characterization. *NeurIPS*.
- Boyd, J. H., Hu, J. & Jagannathan, R. (2005). The stock market's reaction to
  unemployment news: why bad news is usually good for stocks. *Journal of
  Finance* 60(2), 649–672.
- Chandrasekaran, V., Parrilo, P. A. & Willsky, A. S. (2012). Latent variable
  graphical model selection via convex optimization. *Annals of Statistics*
  40(4), 1935–1967.
- Chow, C. K. & Liu, C. N. (1968). Approximating discrete probability
  distributions with dependence trees. *IEEE Transactions on Information
  Theory* 14(3), 462–467.
- Dahlhaus, R. & Eichler, M. (2003). Causality and graphical models in time
  series analysis. In Green, Hjort & Richardson (eds.), *Highly Structured
  Stochastic Systems*, Oxford University Press.
- Danaher, P., Wang, P. & Witten, D. M. (2014). The joint graphical lasso for
  inverse covariance estimation across multiple classes. *JRSS B* 76(2),
  373–397.
- Engelke, S. & Hitz, A. S. (2020). Graphical models for extremes (with
  discussion). *JRSS B* 82(4), 871–932.
- Földiák, P. (1990). Forming sparse representations by local anti-Hebbian
  learning. *Biological Cybernetics* 64(2), 165–170.
- Friedman, J., Hastie, T. & Tibshirani, R. (2008). Sparse inverse covariance
  estimation with the graphical lasso. *Biostatistics* 9(3), 432–441.
- Ganter, B. & Wille, R. (1999). *Formal Concept Analysis: Mathematical
  Foundations*. Springer.
- Gerhardus, A. & Runge, J. (2020). High-recall causal discovery for
  autocorrelated time series with latent confounders. *NeurIPS*.
- Gneiting, T. & Raftery, A. E. (2007). Strictly proper scoring rules,
  prediction, and estimation. *JASA* 102(477), 359–378.
- Grünwald, P. D. (2007). *The Minimum Description Length Principle*. MIT
  Press.
- Gürkaynak, R. S., Sack, B. & Swanson, E. T. (2005). Do actions speak louder
  than words? The response of asset prices to monetary policy actions and
  statements. *International Journal of Central Banking* 1(1), 55–93.
- Hallac, D., Park, Y., Boyd, S. & Leskovec, J. (2017). Network inference via
  the time-varying graphical lasso. *KDD*.
- Hoff, P. D. (2007). Extending the rank likelihood for semiparametric copula
  estimation. *Annals of Applied Statistics* 1(1), 265–283.
- Huang, B., Zhang, K., Zhang, J., Ramsey, J., Sanchez-Romero, R., Glymour, C.
  & Schölkopf, B. (2020). Causal discovery from heterogeneous/nonstationary
  data. *JMLR* 21(89), 1–53.
- Hyvärinen, A., Zhang, K., Shimizu, S. & Hoyer, P. O. (2010). Estimation of
  a structural vector autoregression model using non-Gaussianity. *JMLR* 11,
  1709–1731.
- Janková, J. & van de Geer, S. (2015). Confidence intervals for
  high-dimensional inverse covariance estimation. *Electronic Journal of
  Statistics* 9(1), 1205–1229.
- Joe, H. (2014). *Dependence Modeling with Copulas*. CRC Press.
- Kipf, T., Fetaya, E., Wang, K.-C., Welling, M. & Zemel, R. (2018). Neural
  relational inference for interacting systems. *ICML*.
- Kolar, M., Parikh, A. P. & Xing, E. P. (2010). On sparse nonparametric
  conditional covariance selection. *ICML*.
- Kritzman, M., Li, Y., Page, S. & Rigobon, R. (2011). Principal components as
  a measure of systemic risk. *Journal of Portfolio Management* 37(4),
  112–126.
- Liu, H., Lafferty, J. & Wasserman, L. (2009). The nonparanormal:
  semiparametric estimation of high dimensional undirected graphs. *JMLR* 10,
  2295–2328.
- Liu, H., Han, F., Yuan, M., Lafferty, J. & Wasserman, L. (2012).
  High-dimensional semiparametric Gaussian copula graphical models. *Annals
  of Statistics* 40(4), 2293–2326.
- Löwe, S., Madras, D., Zemel, R. & Welling, M. (2022). Amortized causal
  discovery: learning to infer causal graphs from time-series data. *CLeaR*.
- Mazumder, R. & Hastie, T. (2012). Exact covariance thresholding into
  connected components for large-scale graphical lasso. *JMLR* 13, 781–794.
- Meinshausen, N. & Bühlmann, P. (2006). High-dimensional graphs and variable
  selection with the lasso. *Annals of Statistics* 34(3), 1436–1462.
- Meinshausen, N. & Bühlmann, P. (2010). Stability selection. *JRSS B* 72(4),
  417–473.
- Mooij, J. M., Magliacane, S. & Claassen, T. (2020). Joint causal inference
  from multiple contexts. *JMLR* 21(99), 1–108.
- Pamfil, R. et al. (2020). DYNOTEARS: structure learning from time-series
  data. *AISTATS*.
- Patton, A. J. (2006). Modelling asymmetric exchange rate dependence.
  *International Economic Review* 47(2), 527–556.
- Patton, A. J. (2012). A review of copula models for economic time series.
  *Journal of Multivariate Analysis* 110, 4–18.
- Peters, J., Bühlmann, P. & Meinshausen, N. (2016). Causal inference by using
  invariant prediction: identification and confidence intervals. *JRSS B*
  78(5), 947–1012.
- Rao, R. P. N. & Ballard, D. H. (1999). Predictive coding in the visual
  cortex: a functional interpretation of some extra-classical receptive-field
  effects. *Nature Neuroscience* 2(1), 79–87.
- Ravikumar, P., Wainwright, M. J., Raskutti, G. & Yu, B. (2011).
  High-dimensional covariance estimation by minimizing ℓ1-penalized
  log-determinant divergence. *Electronic Journal of Statistics* 5, 935–980.
- Reisach, A. G., Seiler, C. & Weichwald, S. (2021). Beware of the simulated
  DAG! Causal discovery benchmarks may be easy to game. *NeurIPS*.
- Runge, J. (2020). Discovering contemporaneous and lagged causal relations in
  autocorrelated nonlinear time series datasets. *UAI*.
- Runge, J., Nowack, P., Kretschmer, M., Flaxman, S. & Sejdinovic, D. (2019).
  Detecting and quantifying causal associations in large nonlinear time
  series datasets. *Science Advances* 5(11), eaau4996.
- Schölkopf, B., Locatello, F., Bauer, S., Ke, N. R., Kalchbrenner, N.,
  Goyal, A. & Bengio, Y. (2021). Toward causal representation learning.
  *Proceedings of the IEEE* 109(5), 612–634.
- Shimizu, S., Hoyer, P. O., Hyvärinen, A. & Kerminen, A. (2006). A linear
  non-Gaussian acyclic model for causal discovery. *JMLR* 7, 2003–2030.
- Stock, J. H. & Watson, M. W. (1988). Testing for common trends. *JASA*
  83(404), 1097–1107.
- Stock, J. H. & Watson, M. W. (2002). Macroeconomic forecasting using
  diffusion indexes. *Journal of Business & Economic Statistics* 20(2),
  147–162.
- Tan, V. Y. F., Anandkumar, A. & Willsky, A. S. (2011). Learning
  high-dimensional Markov forest distributions: analysis of error rates.
  *JMLR* 12, 1617–1653.
- Watanabe, S. (1960). Information theoretical analysis of multivariate
  correlation. *IBM Journal of Research and Development* 4(1), 66–82.
- Witten, D. M., Friedman, J. H. & Simon, N. (2011). New insights and faster
  computations for the graphical lasso. *Journal of Computational and
  Graphical Statistics* 20(4), 892–900.
- Xue, L. & Zou, H. (2012). Regularized rank-based estimation of
  high-dimensional nonparanormal graphical models. *Annals of Statistics*
  40(5), 2541–2571.
- Zheng, X., Aragam, B., Ravikumar, P. & Xing, E. P. (2018). DAGs with NO
  TEARS: continuous optimization for structure learning. *NeurIPS*.
- Zhou, S., Lafferty, J. & Wasserman, L. (2010). Time varying undirected
  graphs. *Machine Learning* 80(2–3), 295–319.
