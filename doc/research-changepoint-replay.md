# Replay: change-point model vs the Layer-0 count model (USGS seismic counts)

Date: 2026-09-27 · Status: first experiments: a Poisson-only change-point model, then one with a burst factor; pooling over the H3 tree (2026-10-01), with Layer 0's own model (2026-10-02)

## Question

The count model (`layer0/count.py`, `BayesianCount`) forgets old evidence at a
fixed rate (`memory_seconds`, 3 days by default): a long memory gives a precise
baseline but adapts slowly after a real change, a short one adapts fast but is
noisy. A change-point model (Bayesian online change-point detection, bayesbin's
`ChangePointStream`) infers the averaging window instead: its posterior over the
time since the last change is long in quiet periods and collapses to a few
intervals when a burst starts. Does that help Worldwatch's detection, and is it
calibrated on real feeds?

## Method

- **Data**: the USGS catalogue, M >= 1.0 (the `usgs_seismic` stanza's filter),
  2026-06-27 to 2026-09-27 (24,500 events), as that stream is modelled: counts per
  5-minute window (`native_seconds = 300`) per H3 resolution-3 cell. Streams: the
  world total and the 12 busiest cells (0.012-0.047 events per window).
- **Current model**: `BayesianCount` with the stanza's defaults (no seasonal
  factors, 3-day memory, the burstiness grid).
- **Change-point model, Poisson**: `ChangePointStream.poisson`, Poisson segments
  only, prior Gamma(1, 1/m) with m the mean of a 2-day warm-up, expected segment
  length one week, bayesbin 0.3.0 defaults otherwise.
- **Change-point model, burst factor**: `ChangePointStream.overdispersed` (bayesbin
  `main` at 3a41d14, not yet released): the same, with negative binomial segments
  averaged over the count model's dispersion grid (0.25..64, inf), weighted by
  their marginal likelihoods. The table shows each stream's most probable
  dispersion r (the NB size per 5-minute window).
- **Scoring**: every window scored before the update; the randomized PIT for
  calibration; the conservative `q_detect` of ADR 0003 for alarms (upper tail,
  q_detect >= 0.999: nominally 0.29 per day per stream); the first 2 days skipped.

Scripts: `research/replay_changepoint/` (fetch, prepare, replay, report).

## Calibration and alarms

| stream | events/window | model | KS D | P(q>0.99) | P(q>0.999) | P(q<0.01) | alarms/day |
|---|---|---|---|---|---|---|---|
| world | 0.925 | current | 0.006 | 0.0106 | 0.00162 | 0.0100 | 0.28 |
|  | 0.925 | change-point, Poisson | 0.011 | 0.0105 | 0.00131 | 0.0110 | 0.21 |
|  | 0.925 | change-point, burst factor (r = 32) | 0.009 | 0.0098 | 0.00116 | 0.0104 | 0.20 |
| 832831fffffffff | 0.047 | current | 0.004 | 0.0113 | 0.00131 | 0.0103 | 0.08 |
|  | 0.047 | change-point, Poisson | 0.005 | 0.0101 | 0.00204 | 0.0106 | 0.26 |
|  | 0.047 | change-point, burst factor (r = 0.5) | 0.005 | 0.0091 | 0.00116 | 0.0106 | 0.04 |
| 835d13fffffffff | 0.047 | current | 0.008 | 0.0098 | 0.00123 | 0.0089 | 0.14 |
|  | 0.047 | change-point, Poisson | 0.008 | 0.0116 | 0.00212 | 0.0089 | 0.34 |
|  | 0.047 | change-point, burst factor (r = 0.5) | 0.005 | 0.0096 | 0.00085 | 0.0103 | 0.11 |
| 8348d4fffffffff | 0.036 | current | 0.005 | 0.0109 | 0.00139 | 0.0099 | 0.18 |
|  | 0.036 | change-point, Poisson | 0.005 | 0.0132 | 0.00228 | 0.0097 | 0.46 |
|  | 0.036 | change-point, burst factor (r = 0.5) | 0.006 | 0.0110 | 0.00139 | 0.0104 | 0.16 |
| 832993fffffffff | 0.033 | current | 0.009 | 0.0109 | 0.00201 | 0.0094 | 0.17 |
|  | 0.033 | change-point, Poisson | 0.015 | 0.0179 | 0.00413 | 0.0094 | 0.52 |
|  | 0.033 | change-point, burst factor (r = 0.5) | 0.015 | 0.0180 | 0.00278 | 0.0100 | 0.23 |
| 834cc5fffffffff | 0.022 | current | 0.005 | 0.0106 | 0.00154 | 0.0096 | 0.20 |
|  | 0.022 | change-point, Poisson | 0.006 | 0.0106 | 0.00158 | 0.0095 | 0.16 |
|  | 0.022 | change-point, burst factor (r = 1) | 0.007 | 0.0103 | 0.00108 | 0.0103 | 0.11 |
| 830c50fffffffff | 0.020 | current | 0.005 | 0.0094 | 0.00066 | 0.0090 | 0.03 |
|  | 0.020 | change-point, Poisson | 0.005 | 0.0097 | 0.00089 | 0.0091 | 0.03 |
|  | 0.020 | change-point, burst factor (r = inf) | 0.005 | 0.0100 | 0.00069 | 0.0102 | 0.03 |
| 8329a6fffffffff | 0.015 | current | 0.005 | 0.0092 | 0.00120 | 0.0103 | 0.11 |
|  | 0.015 | change-point, Poisson | 0.005 | 0.0097 | 0.00135 | 0.0104 | 0.11 |
|  | 0.015 | change-point, burst factor (r = 0.5) | 0.004 | 0.0103 | 0.00096 | 0.0095 | 0.11 |
| 8329a0fffffffff | 0.015 | current | 0.007 | 0.0098 | 0.00139 | 0.0099 | 0.19 |
|  | 0.015 | change-point, Poisson | 0.007 | 0.0110 | 0.00158 | 0.0098 | 0.19 |
|  | 0.015 | change-point, burst factor (r = 0.25) | 0.006 | 0.0106 | 0.00120 | 0.0113 | 0.19 |
| 8322c4fffffffff | 0.014 | current | 0.008 | 0.0094 | 0.00120 | 0.0103 | 0.07 |
|  | 0.014 | change-point, Poisson | 0.004 | 0.0112 | 0.00170 | 0.0102 | 0.09 |
|  | 0.014 | change-point, burst factor (r = 0.25) | 0.006 | 0.0111 | 0.00131 | 0.0104 | 0.04 |
| 8329a9fffffffff | 0.013 | current | 0.005 | 0.0103 | 0.00112 | 0.0113 | 0.10 |
|  | 0.013 | change-point, Poisson | 0.004 | 0.0125 | 0.00154 | 0.0114 | 0.13 |
|  | 0.013 | change-point, burst factor (r = 0.25) | 0.004 | 0.0124 | 0.00131 | 0.0088 | 0.10 |
| 8329a3fffffffff | 0.013 | current | 0.007 | 0.0100 | 0.00131 | 0.0100 | 0.17 |
|  | 0.013 | change-point, Poisson | 0.007 | 0.0110 | 0.00127 | 0.0100 | 0.17 |
|  | 0.013 | change-point, burst factor (r = 0.25) | 0.006 | 0.0107 | 0.00127 | 0.0111 | 0.14 |
| 834882fffffffff | 0.012 | current | 0.007 | 0.0097 | 0.00123 | 0.0099 | 0.12 |
|  | 0.012 | change-point, Poisson | 0.008 | 0.0115 | 0.00170 | 0.0098 | 0.11 |
|  | 0.012 | change-point, burst factor (r = 1) | 0.008 | 0.0120 | 0.00166 | 0.0109 | 0.10 |

Nominal: P(q > 0.99) = 0.01, P(q > 0.999) = 0.001, P(q < 0.01) = 0.01.

All three are close to uniform overall (KS D <= 0.015). In the upper tail the
current model is near nominal (P(q > 0.999) 0.0007-0.0020). The Poisson-only
change-point model runs hot on the busier cells, up to 0.0041 (4x), and alarms
2-3x as often there: the counts are mildly overdispersed (index of dispersion
1.0-1.5 per window), which Poisson segments cannot absorb. With the burst factor
the upper tail comes to the current model's level (0.0007-0.0028) and the model
alarms no more often than the current one on 12 of the 13 streams, less on
several (0.04 a day against 0.08, 0.11 against 0.20). The exception is cell
832993, where both change-point variants run hot at the 1% level (0.018 against
0.011). The learnt dispersions are sensible: r = 0.25-1 for the sparse cells
(at their rates, an index of dispersion of ~1.1), r = 32 for the world total.

## The Alaska sequence (cell 8322c4, south of Nikolski)

A swarm from 2026-09-01 06:44 (M5.3), an M6.3 on 09-03 11:17 and its aftershocks.
Per 6 hours: events, each model's alarms, the events it expected (its predictive
mean summed), and the Poisson change-point model's averaging window (its
posterior mean run length).

| 6 h from | events | current: alarms | current: expected | Poisson c-p: alarms | Poisson c-p: expected | burst factor: alarms | burst factor: expected | averaging window (h) |
|---|---|---|---|---|---|---|---|---|
| 08-30 00:00 | 1 | 0 | 0.3 | 0 | 0.2 | 0 | 0.2 | 100.9 |
| 08-30 06:00 | 0 | 0 | 0.3 | 0 | 0.3 | 0 | 0.3 | 113.8 |
| 08-30 12:00 | 0 | 0 | 0.3 | 0 | 0.2 | 0 | 0.2 | 112.2 |
| 08-30 18:00 | 0 | 0 | 0.3 | 0 | 0.2 | 0 | 0.2 | 110.4 |
| 08-31 00:00 | 0 | 0 | 0.2 | 0 | 0.2 | 0 | 0.2 | 108.6 |
| 08-31 06:00 | 0 | 0 | 0.2 | 0 | 0.2 | 0 | 0.2 | 106.9 |
| 08-31 12:00 | 0 | 0 | 0.2 | 0 | 0.2 | 0 | 0.2 | 105.5 |
| 08-31 18:00 | 0 | 0 | 0.2 | 0 | 0.2 | 0 | 0.2 | 104.4 |
| 09-01 00:00 | 0 | 0 | 0.2 | 0 | 0.1 | 0 | 0.1 | 103.8 |
| 09-01 06:00 | 19 | 3 | 1.2 | 2 | 2.8 | 1 | 2.8 | 25.1 |
| 09-01 12:00 | 3 | 0 | 1.6 | 0 | 3.7 | 0 | 3.7 | 9.9 |
| 09-01 18:00 | 4 | 0 | 1.8 | 0 | 3.6 | 0 | 3.5 | 15.8 |
| 09-02 00:00 | 12 | 1 | 2.0 | 0 | 3.7 | 0 | 3.7 | 21.7 |
| 09-02 06:00 | 20 | 0 | 3.3 | 0 | 5.9 | 0 | 5.8 | 27.3 |
| 09-02 12:00 | 1 | 0 | 3.8 | 0 | 5.4 | 0 | 5.5 | 28.5 |
| 09-02 18:00 | 3 | 0 | 3.7 | 0 | 5.1 | 0 | 5.1 | 34.2 |
| 09-03 00:00 | 1 | 0 | 3.5 | 0 | 2.3 | 0 | 2.7 | 21.4 |
| 09-03 06:00 | 10 | 1 | 3.3 | 2 | 1.1 | 1 | 1.3 | 19.3 |
| 09-03 12:00 | 37 | 0 | 5.0 | 0 | 6.9 | 0 | 6.9 | 57.3 |
| 09-03 18:00 | 34 | 0 | 7.2 | 0 | 8.7 | 0 | 8.7 | 62.7 |
| 09-04 00:00 | 25 | 0 | 9.0 | 0 | 10.2 | 0 | 10.2 | 68.5 |
| 09-04 06:00 | 12 | 0 | 9.9 | 0 | 10.7 | 0 | 10.7 | 74.4 |
| 09-04 12:00 | 7 | 0 | 9.9 | 0 | 10.4 | 0 | 10.4 | 79.1 |
| 09-04 18:00 | 0 | 0 | 9.3 | 0 | 5.0 | 0 | 5.5 | 41.3 |
| 09-05 00:00 | 6 | 0 | 8.7 | 0 | 3.8 | 0 | 4.2 | 40.7 |
| 09-05 06:00 | 3 | 0 | 8.5 | 0 | 8.0 | 0 | 8.7 | 81.5 |
| 09-05 12:00 | 5 | 0 | 8.2 | 0 | 7.6 | 0 | 8.5 | 85.4 |
| 09-05 18:00 | 2 | 0 | 7.9 | 0 | 5.5 | 0 | 6.2 | 65.4 |
| 09-06 00:00 | 4 | 0 | 7.4 | 0 | 1.7 | 0 | 1.9 | 28.3 |
| 09-06 06:00 | 4 | 0 | 7.1 | 0 | 2.3 | 0 | 2.6 | 39.8 |
| 09-06 12:00 | 3 | 0 | 6.9 | 0 | 2.5 | 0 | 2.7 | 45.9 |
| 09-06 18:00 | 4 | 0 | 6.6 | 0 | 2.3 | 0 | 2.3 | 49.9 |
| 09-07 00:00 | 2 | 0 | 6.4 | 0 | 2.5 | 0 | 2.5 | 57.3 |
| 09-07 06:00 | 2 | 0 | 6.1 | 0 | 2.4 | 0 | 2.4 | 61.3 |
| 09-07 12:00 | 4 | 0 | 6.0 | 0 | 2.6 | 0 | 2.6 | 69.2 |
| 09-07 18:00 | 0 | 0 | 5.6 | 0 | 2.1 | 0 | 2.1 | 62.0 |
| 09-08 00:00 | 0 | 0 | 5.2 | 0 | 0.9 | 0 | 1.0 | 33.5 |
| 09-08 06:00 | 3 | 0 | 4.9 | 0 | 1.8 | 0 | 1.8 | 68.3 |
| 09-08 12:00 | 1 | 0 | 4.6 | 0 | 1.7 | 0 | 1.7 | 70.4 |
| 09-08 18:00 | 9 | 0 | 4.6 | 1 | 2.4 | 0 | 2.4 | 98.4 |

current: first alarm after 09-01 00:00: 09-01 07:30; after the M6.3 (09-03 11:17): first 09-03 11:35, 1 alarm windows in the next 3 days

change-point, Poisson: first alarm after 09-01 00:00: 09-01 07:30; after the M6.3 (09-03 11:17): first 09-03 11:20, 2 alarm windows in the next 3 days

change-point, burst factor: first alarm after 09-01 00:00: 09-01 07:30; after the M6.3 (09-03 11:17): first 09-03 11:20, 1 alarm windows in the next 3 days

- **Onset**: both alarm first at 07:30.
- **The M6.3**: the change-point model alarms at 11:20, 15 minutes before the
  current model (11:35): it had concluded the swarm had paused (its averaging
  window had shortened to ~20 h and its expectation fallen), so the renewed burst
  was more surprising.
- **After the sequence**: by 09-06 the actual rate is ~3-4 per 6 h. The
  change-point model expects 1.7-2.5, having adapted within about a day; the
  current model still expects 6-7, 2-3x too many, for days (its 3-day memory), and
  is desensitised: the flare-up of 09-08 18:00 (9 events) alarmed only in the
  change-point model.
- **The averaging window** adapts as intended: ~100-110 h while quiet, 10-25 h
  at the swarm's onset, growing again through the aftershock segment.
- **With the burst factor** the change-point model keeps all of this (the same
  onset alarm, 11:20 on the M6.3, the same recovery) except the flare-up of
  09-08: 9 events spread over 6 hours count as burstiness now, not a change.

## Conclusions

1. The adaptive window works as hoped on the one real burst: the same first
   alarm, 15 minutes' lead on the main shock, and far faster recovery of
   sensitivity afterwards (about a day, against several).
2. Poisson-only segments are not calibrated enough in the upper tail on these
   mildly overdispersed counts. With a burst factor (negative binomial segments
   over a grid of dispersions) the change-point model's upper tail and alarm rate
   match or beat the current count model's on 12 of 13 streams, and it keeps
   the lead and the recovery.
3. Seasonality was not tested: seismic counts have little. For weekly and annual
   cycles the change-point model needs the seasonal profile as its exposure (as
   `BayesianCount` uses `f_hour · f_dow`); otherwise every regular cycle reads
   as a change.

## Caveats

One real sequence; sparse cells; no labelled events, so no detection-delay
statistics at matched false-alarm rates; settings untuned (expected segment
length, prior); the M1+ catalogue is dominated by US networks.

## Next

1. Cell 832993: why both change-point variants run hot at the 1% level there.
2. Injected changes into these real streams (steps of known size and time) for
   detection delay at matched false-alarm rates, all three models.
3. A seasonal exposure, and a feed with strong weekly cycles (GDELT, or a count
   stream built from Wikipedia views).
4. Cost: the burst-factor model runs ten streams per series, 7.6 ms per window
   in this replay (the current and Poisson-only models together took 2.9 ms): fine
   for 5-minute windows, but worth a coarser grid, or one dispersion per stream
   once it has been learnt.

## Pooling over the H3 tree (2026-10-01)

The streams above are single cells. A sparse cell's baseline is noisy; pooling
it with its neighbours helps if they share its rate and hurts if they do not.
Over the H3 tree the data can decide, region by region: each node is one bin,
or its children differ (bayesbin's
[docs/NOTES.md](https://github.com/petfold/bayesbin/blob/main/docs/NOTES.md#where-the-method-stops-being-1-d)).
Does that, with a change-point stream per node, predict the cells' counts
better?

**Method** (`research/replay_changepoint/tree_replay.py`):

- Nodes: the resolution-0 cells that hold the replay's 12 cells (6 of them:
  California, Nevada and Utah; Texas; Hawaii; Puerto Rico; southern Alaska; the
  Aleutians), their resolution-3 cells with events (378) and every ancestor:
  506 nodes, 18,810 of the 24,500 events. The world itself is taken to split.
- Each node runs `ChangePointStream.poisson` on its region's summed 5-minute
  counts, set up as `replay.py` sets up its streams: prior Gamma(1, 1/m), m the
  node's 2-day warm-up mean (at least 0.5 events per warm-up), expected segment
  a week. A child region with no events is one empty bin.
- After each window the tree recursion on the streams' running evidences gives
  P(ν is cell c's bin | data so far), for c and its four ancestors (ρ = 0.1 and
  0.5). The evidence of the finest cells' counts is the stream's marginal plus
  Σ (log Y! − Y log area): the summed count's own Poisson normaliser out, the
  allocation to the cells in.
- The cell's predictive for the next window is the mixture of its five
  candidates' predictives, each at the cell's share of the candidate's area,
  scored before the update as `replay.py` scores (randomized PIT, `q_detect`,
  alarms at 0.999), plus the log predictive probability.
- Baselines: the cell's own stream, which is `replay.py`'s Poisson change-point
  model (its alarms reproduce the table above, cell by cell), and fixed pooling
  with the cell's resolution-2 or resolution-1 ancestor.
- Scored: the replay's 12 cells, and 40 sparse cells drawn from the same
  subtrees (3-30 events in 3 months).

**The 12 busiest cells** (pooled; log score per cell and day, against the cell
alone):

| model | KS D | P(q>0.99) | P(q>0.999) | alarms/day per cell | log score |
|---|---|---|---|---|---|
| cell alone | 0.002 | 0.0117 | 0.00173 | 0.21 | |
| fixed pooling, resolution 2 | 0.017 | 0.0263 | 0.00565 | 0.49 | −5.74 |
| fixed pooling, resolution 1 | 0.021 | 0.0303 | 0.01440 | 1.91 | −13.64 |
| tree, ρ = 0.1 | 0.002 | 0.0117 | 0.00173 | 0.21 | −0.00 |
| tree, ρ = 0.5 | 0.002 | 0.0117 | 0.00174 | 0.21 | −0.00 |

**40 sparse cells** (pooled):

| model | KS D | P(q>0.99) | P(q>0.999) | alarms/day per cell | log score |
|---|---|---|---|---|---|
| cell alone | 0.000 | 0.0097 | 0.00066 | 0.07 | |
| fixed pooling, resolution 2 | 0.001 | 0.0095 | 0.00078 | 0.07 | −0.13 |
| fixed pooling, resolution 1 | 0.001 | 0.0095 | 0.00072 | 0.06 | −0.11 |
| tree, ρ = 0.1 | 0.001 | 0.0099 | 0.00082 | 0.08 | +0.00 |
| tree, ρ = 0.5 | 0.001 | 0.0099 | 0.00084 | 0.08 | +0.00 |

- **The busy cells are never pooled.** Their weight on the cell itself is 1.00
  in every window, through the Alaska sequence too, so their predictions,
  calibration and alarms are the cell model's to the digit (first alarm 09-01
  07:30, and 09-03 11:20 after the M6.3, as above). Fixed pooling dilutes them:
  2.3 times the alarms at resolution 2 and 9 times at resolution 1, and 5.7 and
  13.6 nats a day worse per cell.
- **The sparse cells are.** With ρ = 0.1, 0.60 of their weight stays on the
  cell on average and 0.37 goes to the resolution-2 parent; half of them have
  weight below 0.5 on themselves in more than a tenth of the windows. Over 90
  days pooling gains the sparsest (3-5 events) 2.8 nats each, costs the 6-12
  event cells 0.4 and the 13-30 event cells 1.4: +15.5 nats for the 40, too
  little to show per day. The upper tail runs a little hotter (P(q > 0.999)
  0.00082 against 0.00066; 0.08 alarms a day against 0.07).
- ρ hardly matters (0.1 against 0.5: the same within the digits shown, and
  +15.7 nats for the sparse cells).
- Cost: 86 minutes on 4 cores for the 3-month replay (506 nodes × 26,496
  windows, about 1 ms per node and window under load, most of it the stream's
  merging of old run lengths); the tree's weights and mixtures, 3-4 s per ρ.
  Live, the world's 2,798 nodes would take 2-3 s of one core per 5-minute
  window.

**Conclusions.**

1. On this catalogue pooling over the tree changes little. The busy cells keep
   their own rates, since the evidence says their neighbours differ, and for the
   sparse cells gains and losses nearly cancel. The cell's own change-point
   stream with its warm-up prior is already a strong baseline: a long quiet
   stretch pins a low rate down without help.
2. The tree is safe where fixed pooling is not. It reproduces the cell model
   where cells differ, while pooling into a fixed coarser cell costs badly.
3. Pooling pays for the sparsest cells, so it should matter more for sources
   whose cells are mostly empty, and early in a stream, than for an established
   catalogue.

**Caveats.** One pooling pattern over the whole history so far (the weights
move with the evidence, but every past count weighs in); priors from each
node's own warm-up; ρ fixed; the world assumed to split; 52 of the 378 cells in
these subtrees scored.

**Next.**

1. All 378 cells scored, which needs faster streams (merging dominates
   bayesbin's per-update cost).
2. A common prior per unit area instead of each node's warm-up prior: weaker
   cell models early on, where pooling should help most.
3. Pooling that differs by epoch (the tree over time blocks), and grouping of
   siblings.

## Pooling Layer 0 over the H3 tree (2026-10-02)

The same pooling with Worldwatch's own count model instead of the change-point
stream, over the whole tree, every cell scored
(`research/replay_changepoint/tree_layer0.py`).

**Method.** Every node of the H3 tree (the 1,708 resolution-3 cells with
events and their 1,090 ancestors; the world taken to split) runs `BayesianCount`
with the `usgs_seismic` stanza's settings on its region's summed 5-minute counts,
vectorized over the nodes and checked against the class itself (3,000 windows ×
12 cells: P(y) within 1.4e-7 relative, no alarm decision different). A cell's
predictive is the mixture of its own and its three ancestors' predictives, each
at the cell's share of the ancestor's area, weighted by P(node is the cell's bin)
from the tree recursion on the nodes' log predictive scores (ρ = 0.1), summed
with a forgetting time of 3 days, 30 days, or none. Every cell with events is
scored in every window after the warm-up (fully observed: zero counts
included), as `replay.py` scores. Ground truth: the 571 quakes of M ≥ 5 after
the warm-up, each in its resolution-3 cell. Layer 0 today (the cell alone)
reproduces the current model's alarms in the table above, cell by cell.
22 minutes on 4 cores.

**By how many events a cell had in the 3 months** (log score per cell and day
against Layer 0 today; nominal P(q > 0.999) = 0.001):

| cells | model | P(q>0.999) | alarms/day per cell | log score |
|---|---|---|---|---|
| 15 busy (300+) | Layer 0 today | 0.00119 | 0.119 | |
| | the resolution-2 cell | 0.00475 | 0.344 | −4.648 |
| | tree, memory 3 days | 0.00122 | 0.127 | −0.014 |
| | tree, memory 30 days | 0.00128 | 0.133 | −0.052 |
| 118 with 31-299 | Layer 0 today | 0.00091 | 0.044 | |
| | the resolution-2 cell | 0.00162 | 0.142 | −0.300 |
| | tree, memory 3 days | 0.00107 | 0.073 | −0.024 |
| | tree, memory 30 days | 0.00112 | 0.093 | −0.080 |
| 514 with 3-30 | Layer 0 today | 0.00051 | 0.045 | |
| | the resolution-2 cell | 0.00095 | 0.065 | +0.009 |
| | tree, memory 3 days | 0.00102 | 0.059 | +0.031 |
| | tree, memory 30 days | 0.00108 | 0.070 | +0.001 |
| 1,061 with 1-2 | Layer 0 today | 0.00044 | 0.013 | |
| | the resolution-2 cell | 0.00090 | 0.013 | +0.108 |
| | tree, memory 3 days | 0.00099 | 0.014 | +0.118 |
| | tree, memory 30 days | 0.00099 | 0.014 | +0.117 |

KS D is at most 0.003 for every model and class (0.015 for the busy cells
pooled into the resolution-2 cell).

**Over the whole catalogue:**

| model | log score vs today, 90 days | alarms/day, all cells | M ≥ 5 quakes alarmed in their window | within an hour |
|---|---|---|---|---|
| Layer 0 today | | 44.3 | 329 of 571 (58%) | 349 |
| the resolution-2 cell | +1,338 nats | 69.7 | 463 (81%) | 478 |
| tree, memory 3 days | +12,448 nats | 55.6 | 409 (72%) | 427 |
| tree, memory 30 days | +10,279 nats | 63.7 | 444 (78%) | 454 |
| tree, no forgetting | +9,539 nats | 67.3 | 468 (82%) | 475 |

- **Sparse cells are too cold today.** With few events, a cell's own model stays
  wide, so its upper tail has half the nominal mass (P(q > 0.999) 0.00044-0.00051
  against 0.001) and a single real event often does not stand out: Layer 0 today
  alarms on only 58% of the M ≥ 5 quakes, which mostly fall in cells with few
  M ≥ 1 events (the catalogue is complete to M 1 only near US networks). Pooled,
  those cells borrow their parents' rates (weight on the cell itself: 0.02 for
  cells with 1-2 events, 0.13 for 3-30), their tail comes to nominal, and 72-82%
  of the big quakes alarm.
- **Busy cells are left alone** (weight 0.98 on the cell): their alarms and log
  score barely move, where pooling them into a fixed coarser cell triples their
  alarms and costs 4.6 nats a day each.
- **The cost** is in the medium cells (31-299 events, weight 0.79 on the cell):
  their upper tail runs a little hot with long memories (0.00112-0.00121) and
  their log score drops slightly; the 3-day memory keeps that smallest.
- **The memory**: 3 days gives the best prediction overall (+138 nats a day) and
  the smallest rise in alarms (+26%); longer memories catch more big quakes at
  more alarms. The extra alarms are mostly calibration restored: today's sparse
  cells give fewer extreme q's than a calibrated model should.
- The Alaska cell is not pooled through its sequence (first alarm 09-01 07:30,
  then 09-03 11:35 after the M6.3, as today).

**Conclusion.** Pooling Layer 0 over the H3 tree is worth having: it improves the
predictive distribution of the whole catalogue, calibrates the sparse cells'
upper tail and makes single notable events in sparse cells stand out, while
leaving the busy cells' models as they are. A 3-day forgetting time is the
default to start with. Implemented as an opt-in for count stanzas
(`[<stanza>.model] pool = "h3"`, ADR 0005), on for `usgs_seismic`.
