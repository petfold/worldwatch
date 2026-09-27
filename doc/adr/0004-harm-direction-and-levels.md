# ADR 0004 — Alerting on harm: tail direction and harm levels

Date: 2026-09-27 · Status: accepted

## Context

Detection is calibrated surprise: every reading is judged against its own
sensor's learned normal. Two things follow that are right for detection and
wrong for alerting a person:

- **Both directions count.** On 2026-09-27 both pushes of the day were carried
  by radiation stations reading 0.002–0.004 µSv/h *below* their normal. For a
  harmful substance or effect there is no "too low".
- **Unusual is not dangerous.** A station 5% above its own normal is surprising
  and harmless. Stations in the Chernobyl exclusion zone read 1–5 µSv/h every
  hour and are rightly not surprising, yet a jump there matters.

## Decision

- **Tail direction** (`[alerts] tail = "upper" | "lower"`, default both): for a
  stream that can only do harm one way, the other way is no evidence. The
  one-sided tail p replaces the two-sided one in the CUSUM, the single-source
  test, the extreme-stanza test and `alert_score`. Under H0 it is uniform too, so
  the CUSUM stays calibrated. Upper: radiation, unreachable probes, earthquake
  counts, weather warnings, IODA alert counts. Lower: internet traffic (a drop is
  the outage). Both: night lights (dark is a power cut, bright can be fire),
  markets, attention. Layer 0 and the surprise archive are unchanged: both tails
  are still scored and kept.
- **Harm levels** (`[alerts] harm_levels = [[value, words], ...]`) in the
  source's units, judged by the notifier (`worldwatch.api.harm`), never by the
  alert engine or Layer 1. They need the observed value, which the interchange
  contract keeps out of detection; the notifier already reads it to say what
  happened (guardrail 8). A signal below the first level (`harm_floor`, default
  true) does not count for pushing: an alert with nothing else is kept on the
  dashboard and never pushed. A level confirmed by `min_sensors` cells (or on an
  alert two kinds of measurement already confirm) raises the stage:
  `harm_confirmed` to at least Confirmed, pushed past the budget, and
  `harm_extreme` to Extreme (which may wake, within reach). Radiation: 0.3 µSv/h
  (above natural background), then 1, 100 and 1000 µSv/h (the IAEA default
  operational intervention levels). Earthquakes: M6 and M7 by the bin's largest
  magnitude, with no floor, since a swarm of small quakes is still evidence.

## Consequences

- Harmless-direction and below-harm deviations are no longer pushed. They stay
  in the surprise archive and on the dashboard, and they are exactly what a
  periodic review should look at (a drop in pollution can mean a factory
  stopped): see the weekly report.
- Harm is judged from the raw reading while it is in `raw_ring`, else the
  consolidated bin.
