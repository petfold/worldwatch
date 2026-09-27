# Replay: change-point model vs the Layer-0 count model (USGS seismic counts)

Date: 2026-09-27 · Status: first experiment (Poisson-only change-point variant)

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
- **Change-point model**: `ChangePointStream.poisson`, **Poisson segments only**
  (no burst factor), prior Gamma(1, 1/m) with m the mean of a 2-day warm-up,
  expected segment length one week, bayesbin 0.3.0 defaults otherwise.
- **Scoring**: every window scored before the update; the randomized PIT for
  calibration; the conservative `q_detect` of ADR 0003 for alarms (upper tail,
  q_detect >= 0.999: nominally 0.29 per day per stream); the first 2 days skipped.

Scripts: `research/replay_changepoint/` (fetch, prepare, replay, report).

## Calibration and alarms

| stream | events/window | model | KS D | P(q>0.99) | P(q>0.999) | P(q<0.01) | alarms/day |
|---|---|---|---|---|---|---|---|
| world | 0.925 | current | 0.006 | 0.0106 | 0.00162 | 0.0100 | 0.28 |
|  | 0.925 | change-point | 0.011 | 0.0105 | 0.00131 | 0.0110 | 0.21 |
| 832831fffffffff | 0.047 | current | 0.004 | 0.0113 | 0.00131 | 0.0103 | 0.08 |
|  | 0.047 | change-point | 0.005 | 0.0101 | 0.00204 | 0.0106 | 0.26 |
| 835d13fffffffff | 0.047 | current | 0.008 | 0.0098 | 0.00123 | 0.0089 | 0.14 |
|  | 0.047 | change-point | 0.008 | 0.0116 | 0.00212 | 0.0089 | 0.34 |
| 8348d4fffffffff | 0.036 | current | 0.005 | 0.0109 | 0.00139 | 0.0099 | 0.18 |
|  | 0.036 | change-point | 0.005 | 0.0132 | 0.00228 | 0.0097 | 0.46 |
| 832993fffffffff | 0.033 | current | 0.009 | 0.0109 | 0.00201 | 0.0094 | 0.17 |
|  | 0.033 | change-point | 0.015 | 0.0179 | 0.00413 | 0.0094 | 0.52 |
| 834cc5fffffffff | 0.022 | current | 0.005 | 0.0106 | 0.00154 | 0.0096 | 0.20 |
|  | 0.022 | change-point | 0.006 | 0.0106 | 0.00158 | 0.0095 | 0.16 |
| 830c50fffffffff | 0.020 | current | 0.005 | 0.0094 | 0.00066 | 0.0090 | 0.03 |
|  | 0.020 | change-point | 0.005 | 0.0097 | 0.00089 | 0.0091 | 0.03 |
| 8329a6fffffffff | 0.015 | current | 0.005 | 0.0092 | 0.00120 | 0.0103 | 0.11 |
|  | 0.015 | change-point | 0.005 | 0.0097 | 0.00135 | 0.0104 | 0.11 |
| 8329a0fffffffff | 0.015 | current | 0.007 | 0.0098 | 0.00139 | 0.0099 | 0.19 |
|  | 0.015 | change-point | 0.007 | 0.0110 | 0.00158 | 0.0098 | 0.19 |
| 8322c4fffffffff | 0.014 | current | 0.008 | 0.0094 | 0.00120 | 0.0103 | 0.07 |
|  | 0.014 | change-point | 0.004 | 0.0112 | 0.00170 | 0.0102 | 0.09 |
| 8329a9fffffffff | 0.013 | current | 0.005 | 0.0103 | 0.00112 | 0.0113 | 0.10 |
|  | 0.013 | change-point | 0.004 | 0.0125 | 0.00154 | 0.0114 | 0.13 |
| 8329a3fffffffff | 0.013 | current | 0.007 | 0.0100 | 0.00131 | 0.0100 | 0.17 |
|  | 0.013 | change-point | 0.007 | 0.0110 | 0.00127 | 0.0100 | 0.17 |
| 834882fffffffff | 0.012 | current | 0.007 | 0.0097 | 0.00123 | 0.0099 | 0.12 |
|  | 0.012 | change-point | 0.008 | 0.0115 | 0.00170 | 0.0098 | 0.11 |

Nominal: P(q > 0.99) = 0.01, P(q > 0.999) = 0.001, P(q < 0.01) = 0.01.

Both are close to uniform overall (KS D <= 0.015). In the upper tail the current
model is near nominal (0.0007-0.0020); the Poisson-only change-point model runs
hot on the busier cells, up to 0.0041 (4x), and alarms 2-3x as often there (up to
0.5 a day against 0.2). The counts are mildly overdispersed (index of dispersion
1.0-1.5 per window): the current model's burst factor absorbs that; Poisson
segments cannot.

## The Alaska sequence (cell 8322c4, south of Nikolski)

A swarm from 2026-09-01 06:44 (M5.3), an M6.3 on 09-03 11:17 and its aftershocks.
Per 6 hours: events, each model's alarms, the events it expected (its predictive
mean summed), and the change-point model's averaging window (its posterior mean
run length).

| 6 h from | events | current: alarms | current: expected | change-point: alarms | change-point: expected | averaging window (h) |
|---|---|---|---|---|---|---|
| 08-30 00:00 | 1 | 0 | 0.3 | 0 | 0.2 | 100.9 |
| 08-30 06:00 | 0 | 0 | 0.3 | 0 | 0.3 | 113.8 |
| 08-30 12:00 | 0 | 0 | 0.3 | 0 | 0.2 | 112.2 |
| 08-30 18:00 | 0 | 0 | 0.3 | 0 | 0.2 | 110.4 |
| 08-31 00:00 | 0 | 0 | 0.2 | 0 | 0.2 | 108.6 |
| 08-31 06:00 | 0 | 0 | 0.2 | 0 | 0.2 | 106.9 |
| 08-31 12:00 | 0 | 0 | 0.2 | 0 | 0.2 | 105.5 |
| 08-31 18:00 | 0 | 0 | 0.2 | 0 | 0.2 | 104.4 |
| 09-01 00:00 | 0 | 0 | 0.2 | 0 | 0.1 | 103.8 |
| 09-01 06:00 | 19 | 3 | 1.2 | 2 | 2.8 | 25.1 |
| 09-01 12:00 | 3 | 0 | 1.6 | 0 | 3.7 | 9.9 |
| 09-01 18:00 | 4 | 0 | 1.8 | 0 | 3.6 | 15.8 |
| 09-02 00:00 | 12 | 1 | 2.0 | 0 | 3.7 | 21.7 |
| 09-02 06:00 | 20 | 0 | 3.3 | 0 | 5.9 | 27.3 |
| 09-02 12:00 | 1 | 0 | 3.8 | 0 | 5.4 | 28.5 |
| 09-02 18:00 | 3 | 0 | 3.7 | 0 | 5.1 | 34.2 |
| 09-03 00:00 | 1 | 0 | 3.5 | 0 | 2.3 | 21.4 |
| 09-03 06:00 | 10 | 1 | 3.3 | 2 | 1.1 | 19.3 |
| 09-03 12:00 | 37 | 0 | 5.0 | 0 | 6.9 | 57.3 |
| 09-03 18:00 | 34 | 0 | 7.2 | 0 | 8.7 | 62.7 |
| 09-04 00:00 | 25 | 0 | 9.0 | 0 | 10.2 | 68.5 |
| 09-04 06:00 | 12 | 0 | 9.9 | 0 | 10.7 | 74.4 |
| 09-04 12:00 | 7 | 0 | 9.9 | 0 | 10.4 | 79.1 |
| 09-04 18:00 | 0 | 0 | 9.3 | 0 | 5.0 | 41.3 |
| 09-05 00:00 | 6 | 0 | 8.7 | 0 | 3.8 | 40.7 |
| 09-05 06:00 | 3 | 0 | 8.5 | 0 | 8.0 | 81.5 |
| 09-05 12:00 | 5 | 0 | 8.2 | 0 | 7.6 | 85.4 |
| 09-05 18:00 | 2 | 0 | 7.9 | 0 | 5.5 | 65.4 |
| 09-06 00:00 | 4 | 0 | 7.4 | 0 | 1.7 | 28.3 |
| 09-06 06:00 | 4 | 0 | 7.1 | 0 | 2.3 | 39.8 |
| 09-06 12:00 | 3 | 0 | 6.9 | 0 | 2.5 | 45.9 |
| 09-06 18:00 | 4 | 0 | 6.6 | 0 | 2.3 | 49.9 |
| 09-07 00:00 | 2 | 0 | 6.4 | 0 | 2.5 | 57.3 |
| 09-07 06:00 | 2 | 0 | 6.1 | 0 | 2.4 | 61.3 |
| 09-07 12:00 | 4 | 0 | 6.0 | 0 | 2.6 | 69.2 |
| 09-07 18:00 | 0 | 0 | 5.6 | 0 | 2.1 | 62.0 |
| 09-08 00:00 | 0 | 0 | 5.2 | 0 | 0.9 | 33.5 |
| 09-08 06:00 | 3 | 0 | 4.9 | 0 | 1.8 | 68.3 |
| 09-08 12:00 | 1 | 0 | 4.6 | 0 | 1.7 | 70.4 |
| 09-08 18:00 | 9 | 0 | 4.6 | 1 | 2.4 | 98.4 |

current: first alarm after 09-01 00:00: 09-01 07:30; after the M6.3 (09-03 11:17): first 09-03 11:35, 1 alarm windows in the next 3 days

change-point: first alarm after 09-01 00:00: 09-01 07:30; after the M6.3 (09-03 11:17): first 09-03 11:20, 2 alarm windows in the next 3 days

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

## Conclusions

1. The adaptive window works as hoped on the one real burst: the same first
   alarm, 15 minutes' lead on the main shock, and far faster recovery of
   sensitivity afterwards (about a day, against several).
2. Poisson-only segments are not calibrated enough in the upper tail on mildly
   overdispersed counts: 2-4x the nominal tail rate on the busier cells. The
   segments need the current model's burst factor (a negative-binomial mixture)
   before this could replace or join Layer 0; on burstier feeds (GDELT, news) the
   gap would be far larger.
3. Seasonality was not tested: seismic counts have little. For weekly and annual
   cycles the change-point model needs the seasonal profile as its exposure (as
   `BayesianCount` uses `f_hour · f_dow`); otherwise every regular cycle reads
   as a change.

## Caveats

One real sequence; sparse cells; no labelled events, so no detection-delay
statistics at matched false-alarm rates; settings untuned (expected segment
length, prior); the M1+ catalogue is dominated by US networks.

## Next

1. A burst factor in bayesbin's Poisson segments (NB mixture over the dispersion,
   as in `BayesianCount`); rerun this replay: the upper tail must come to nominal.
2. Injected changes into these real streams (steps of known size and time) for
   detection delay at matched false-alarm rates, both models.
3. A seasonal exposure, and a feed with strong weekly cycles (GDELT or a count
   stream built from Wikipedia views).
