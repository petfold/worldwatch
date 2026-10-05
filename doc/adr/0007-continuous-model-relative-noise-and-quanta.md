# ADR 0007 — Continuous model v3: relative process noise, quantized PITs

Date: 2026-10-05 · Status: accepted

## Context

The nursery's first run (ADR 0006) kept 38 of 82 streams out for the shape of
their PITs, nearly all continuous, nearly all the same way: PITs piled in the
middle deciles, no tail mass (Cloudflare Radar, BTC/ETH, river gauges, RIPE
Atlas; TV 0.3-0.7). Such a stream can never alert. Two faults in the
continuous model (`layer0/continuous.py`, v2) explain it.

1. **Process noise in absolute units.** The state covariance is kept in units
   of the learned observation variance S (West & Harrison), but `level_var`,
   `trend_var` and `seasonal_var` were added in absolute units of the data.
   When a stanza's scale guess was off, as with Cloudflare's default
   `obs_scale` of 1.0 for values with ~0.02 noise, the level noise was many
   times the observation noise: a predictive far too wide, every PIT near 0.5.
2. **Recorded resolution.** Values recorded in whole units (gauges in cm,
   counts behind log1p) repeat exactly; a continuous PIT then clumps.

## Evidence

`research/replay_continuous/` replays real histories through the production
model and judges the PITs as the nursery does (fetch.py: public APIs; the
Cloudflare series came from the VPS's token, 28 days). The data stay out of git.

| Stream | v2 TV, tails (lo/hi) | v3 as configured below |
|---|---|---|
| BTC, 14 d of 1-min closes | 0.287, 0.21x/0.22x | 0.009, 0.96x/1.02x |
| ETH, same | 0.335, 0.10x/0.09x | 0.010, 0.85x/0.99x |
| PEGELONLINE, 30 gauges, 15 d hourly | 0.350, 0.02x/0.07x | 0.025, 0.97x/1.20x |
| Cloudflare Radar, 12 streams, 28 d | 0.210, 0.00x/0.00x | 0.015, 1.09x/1.34x |
| GB grid frequency (active; control) | 0.031 | 0.019 |

The replay reproduced production (BTC 0.287 against the archive's 0.307, ETH
0.335 against 0.355, rivers 0.350 against 0.360). Per Cloudflare stream on the
lower tail (the one that alerts): 8 of 12 pass the nursery's test; global, JP and
US have TV 0.053-0.057, Oman's lower tail is 2.0x.

## Decision

**Model v3** (a version bump: every continuous cell starts afresh from its
stanza, and the nursery judges each model on its own PITs, ADR 0006):

- Process noise is relative: Q_t = q · S_t / obs_scale². A stanza's rates hold
  when its scale guess is right and scale with the learned variance when not,
  so a correctly configured stream behaves as before and no stanza has to be
  rewritten in new units.
- `quantum` (raw units) gives a randomized PIT: q uniform between the predictive
  CDF at the quantum's edges, through the stanza's `transform`; `last_detect_q`
  is the least extreme value (as ADR 0003 for counts). The noise variance is
  learned net of the rounding's own (quantum²/12, Sheppard's correction), which
  the randomized PIT already spreads over; without it the tails fall to 0.5x.

**Stanzas**:

| Streams | Change | Why |
|---|---|---|
| `btc_usd`, `eth_usd` | `scale_memory_seconds = 3600`, `obs_dof = 3` | volatility clusters within the hour; minute returns are heavier-tailed than t(4) |
| `pegelonline_water` | `quantum = 1`, `level_var = 3`, `periods_seconds = [44712]` | whole cm; the M2 tide on estuary gauges |
| the 12 `cf_radar_netflows_*` | `obs_scale = 0.02`, `level_var = 1e-4`, daily and weekly harmonics (3 each), `seasonal_var = 1e-6` | a share of peak traffic with a strong daily and weekly shape; the defaults had no harmonics |
| `opensky_aircraft`, `digitraffic_ships`, `aisstream_chokepoints`, `ripe_atlas_probes` | `quantum = 1` | whole counts behind log1p (no history to replay; the synthetic tests cover the case) |

## Not fixed here

- `swpc_xray`: 1-minute log X-ray flux is skewed (flares rise in minutes and
  decay over tens of minutes). No setting of a symmetric-noise model calibrates
  it (best TV 0.14, upper tail 2.5-5.5x, lower 0.4x). It needs a different
  model, flares as events or a skewed jump term; it stays in the nursery.
- Streams with no history to replay (EIA and AEMO demand, OONI, ADS-B GNSS,
  FX, air quality, EURDEP, Wikipedia, the new satellite streams) get v3's
  relative noise and nothing else; the nursery will judge them on live data.
- Underconfident rare-event counts (`nws_extreme`, `usgs_significant`,
  `sigmet_volcanic_ash`) are the count model's, not this one's; they alert on
  every item anyway (ADR 0001).

## Consequences

Every continuous model restarts on deploy and needs about 3 days and 200 PITs
before the nursery can promote it (ADR 0006). The two continuous streams that
were already active (`elexon_frequency`, `bfs_odl_gamma`) stay active while
their new PITs accrue: the drift check needs 200 of them before it judges.
