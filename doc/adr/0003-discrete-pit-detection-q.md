# ADR 0003 — Detection q for discrete observations

Date: 2026-09-26 · Status: accepted

## Context

Count streams (quakes, news events, unreachable probe targets) emit the
randomized PIT, q = F(y−1) + u·P(Y=y) with u ~ U(0,1). It is exactly uniform
under a correct model, which is what calibration checks and Layer 1 need.

As evidence it is wrong. A zero when zero is the likeliest outcome (P(0) = 0.97)
lands at q < 0.005 whenever u < 0.005: the observation says nothing unusual,
the random draw does. On 2026-09-26 this showed as 124 countries turning
"rare, low" at once in the prober stream. That was made worse by one shared
seed for every model (fixed separately: seeds are now per stream and cell),
but even with independent draws a quiet cell reads "1-in-100" about 1% of the
time, for no reason.

## Decision

- The surprise archive gains `q_detect` (schema v8): for a discrete observation
  the value in its attainable PIT interval [F(y−1), F(y)] closest to 0.5,
  i.e. "at least this many" for the upper tail and "at most this many" for
  the lower. NULL means the same as `q_value` (continuous streams).
- `q_value` keeps the randomized PIT, unchanged in meaning.
- Every tail decision and display (CUSUM evidence, alert candidates, rarity
  words, hexagons, pushes) reads `COALESCE(q_detect, q_value)`. The calibration
  line on the dashboard ("in the 1% tails … calibrated ≈ 2%") reads `q_value`.

## Consequences

- A quiet window can no longer be surprising; bursts keep their full extremity.
- Detection on counts is conservative: under a correct model it fires at or
  below the nominal rate, never above it.
- Rows scored before v8 have NULL `q_detect` and keep their randomized value
  as displayed until they age out of the 24 h views.
