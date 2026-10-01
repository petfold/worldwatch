# ADR 0005 — Pooling count streams over the H3 tree

Date: 2026-10-02 · Status: accepted

## Context

Layer 0 models each (stream, cell) on its own. A sparse cell then never learns
its rate: with a 3-day memory, a cell that sees an event a month holds about a
tenth of an event of evidence, so the prior's half-event dominates, the model
over-predicts events, and its upper tail has too little mass. On the USGS
replay (3 months of 5-minute counts, 1,708 resolution-3 cells with events) the
cells with 1-30 events put P(q > 0.999) at 0.00044-0.00051 against the nominal
0.001, and only 58% of the M ≥ 5 quakes alarmed in their own window: most of
them fall in cells with few M ≥ 1 events.

The H3 hierarchy offers a remedy that the data can choose region by region. Each
node of the tree above a stream's cells (each cell and its ancestors up to the
base cells) can run the same count model on its region's summed counts; the
tree recursion

    Z(ν) = ρ S(ν) + (1 − ρ) Π_children Z(χ),     Z = S at a cell,

on the nodes' log predictive scores S says, after every window, how probably
each node is a cell's bin (one rate for its whole region) rather than its
children differing (bayesbin's exact binning over trees). A cell's predictive is
then the mixture of its own and its ancestors' predictives, each at the cell's
share of the ancestor's area.

Replayed with Layer 0's own model on every node
(`research/replay_changepoint/tree_layer0.py`, doc/research-changepoint-replay.md),
with S summed over a 3-day forgetting time and ρ = 0.1:

- the sparse cells' upper tail comes to nominal (P(q > 0.999) 0.00099-0.00102);
- 72% of the M ≥ 5 quakes alarm in their window (58% today; 78-82% with longer
  memories, at more alarms);
- busy cells keep 98% of the weight on themselves and barely move (fixed pooling
  into the resolution-2 cell instead triples their alarms);
- the log predictive score of the whole catalogue rises by 12,448 nats over 90
  days; alarms over all cells rise from 44 to 56 a day, mostly calibration
  restored.

## Decision

- **Opt-in per count stanza**: `[<stanza>.model] pool = "h3"`, with
  `pool_memory_seconds` (default 3 days: the forgetting time of the nodes'
  scores), `pool_rho` (default 0.1: a node's prior probability of being one bin)
  and `pool_coarsest` (default 0: the coarsest resolution pooled over). It
  applies to a stream's H3 cells at its `h3_resolution`; any other cell (a box,
  a country) is scored on its own as before.
- **Live scoring** (`worldwatch.layer0.pool.TreePool`, from
  `LiveScorer._close_windows`): every closed window is scored for all of the
  stream's live cells at once, advancing the cells' own models and their
  ancestors' together. A child region with no live cell counts as one empty
  region (a model fed zeros). A region new to the pool starts with the empty
  region's score: it was empty until then.
- **The model** is BayesianCount's recursions, vectorized over the nodes (gamma
  quantiles from a table, relative error about 1e-7; the tests hold the
  vectorized step to the class). A cell's own model is the BayesianCount Layer 0
  keeps for it anyway, saved where it always was; its random sequence is the one
  it would draw unpooled. Ancestors' models and every node's score are saved in
  `model_state` at scale −2 (`POOL_SCALE`), version 1.
- **Surprise rows** of pooled cells carry `model_version` 102 (100 + the count
  model's version 2): the q's come from a different predictive than the cell's
  own.
- **On for `usgs_seismic`**, the stream it was replayed on. Its role stays
  "context"; detection still uses `usgs_m45`.

## Consequences

- Sparse cells' q's are calibrated and a single notable event in a quiet cell
  stands out; a new cell's first report is judged by its region's rate instead
  of returning 0.5.
- More upper-tail q's from sparse cells, as a calibrated model should give. On
  the fully observed replay alarms at q ≥ 0.999 rose 26%. Through `LiveScorer`
  itself (`research/replay_changepoint/live_replay.py`: a week of the catalogue,
  the Alaska swarm inside it, from an empty database) the change is larger,
  because there sparse cells are new or come back after quiet spells: today
  their first report scores 0.5 and the next ones meet a near-prior model. Today
  that week had P(q > 0.999) 0.00036 (a third of nominal), P(q > 0.99) 0.0068
  and 17 alarms; pooled, 0.00125, 0.0093 and 294 alarms (0.06% of cell-windows,
  below the nominal 0.1% as conservative detection should be), with the Alaska
  cell's alarms at the same times. `usgs_seismic` is context, so these are
  surprise-field values, not alerts.
- Injected swarms (`tree_inject.py`, one extra event an hour for 6 hours): in
  sparse cells 11 of 12 detected against 9 of 12 today (10 at today's false-alarm
  rate), the same in medium cells (6 of 12) and in resolution-2 regions (10 of
  10), median delays unchanged (0.4-1.2 h).
- Switching pooling off resumes every cell from its own model; only the pool's
  rows at scale −2 are left unused.
- Cost: a pooled window over all of `usgs_seismic`'s 1,708 cells takes about
  0.2 s, against about 3 s for scoring them one model at a time; in the live
  replay, 137 ms per tick against 543 ms.
- Open: one pooling pattern per window over every live cell (pooling that
  differs by epoch, and grouping siblings rather than all seven children, are
  not done); other count streams (news, fires) need their own replay first,
  since seasonality and burstiness differ.
