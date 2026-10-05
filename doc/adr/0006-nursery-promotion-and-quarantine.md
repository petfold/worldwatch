# ADR 0006 — The nursery: promotion on calibrated PITs, quarantine on drift

Date: 2026-10-05 · Status: accepted

## Context

The spec (§4) puts every new source in a nursery, in shadow, "until rolling PIT
uniformity test passes → automatic promotion", with drift flagging a model for
quarantine; the pass criteria were left open (§15). P0 shipped the status
column and a cap on nursery streams' single-source severity, nothing more: all
83 sources were still `nursery` on 5 Oct 2026, none promoted, and nursery
streams counted in full toward corroborated alerts. The status was decoration.

The surprise archive (the last 14 days, 65 streams, 6 million native-scale
PITs) shows why a real gate matters. The models fall in three groups:

- **calibrated**: the earthquake counts (USGS, EMSC, M4.5+), IODA outage
  events, the prober, NWS severe warnings: total-variation distance of the
  decile histogram from uniform (TV) under 0.02, 1% tails at 0.6-1.0x their share;
- **overconfident**, false alarms waiting to happen: `swpc_xray` (1% tails at
  6x, 0.1% tails at 40-60x), `adsb_gnss_interference` (upper tail 7x), the
  FIRMS fire counts (lower tail 1.7x, TV 0.2);
- **underconfident**, never able to fire: most continuous streams with a fixed
  observation scale (every Cloudflare Radar country, BTC/ETH, river gauges,
  RIPE Atlas): PITs bunched in the middle, TV 0.3-0.7, no tail mass at all.

A uniformity test alone is the wrong tool. With hundreds of thousands of PITs a
Kolmogorov-Smirnov test rejects every real model; with two hundred, a raw ratio
is noise. Detection lives in the tails, and only in the tail a stream alerts on
(ADR 0004).

## Decision

`worldwatch.layer0.nursery` judges each non-retired stream every 6 hours (a
systemd timer, and at `worldwatch init`, so a deploy restarts on the new
verdicts) from its surprise rows' q_value (the randomized PIT for counts, ADR
0003), and writes the status into the `sources` table. The stanza's status is
only the starting point; `retired` in a stanza is final.

Statistics over a window: n; span; TV of the decile histogram; how often q <= 1%
and q >= 99% against the 1% expected (the ratio). A counted tail ([alerts]
tail: upper, lower or both) is **overconfident** when its ratio is above 2 and
the excess is significant (z > 3, binomial approximation), **underconfident**
when below 0.5, significantly.

| From | To | When |
|---|---|---|
| nursery | active | last 14 days: n >= 200 over >= 3 days, TV <= 0.05, no counted tail over- or underconfident |
| active | quarantined | last 7 days (n >= 200): TV > 0.10, or a counted tail above 3x its share, significantly |
| quarantined | active | the promotion test passes on the last 7 days |

Underconfidence keeps a stream out of the nursery (it is not calibrated, and
it could never fire) but never quarantines an active one: a quiet tail does no
harm. Each verdict is a `calibration` row (90 days kept); each change a health
event (`promoted`, `quarantined`, `released`); `/api/nursery` shows the latest;
the weekly digest counts them.

**The gate.** `alerts.engine.open_alerts` drops the candidates of nursery and
quarantined streams before any policy runs: they neither corroborate nor alert
alone nor escalate an alert. Authoritative every-event alerts (ADR 0001) read
the ingestion keys, not q-values, and are never gated: a tsunami warning does
not wait for a calibration test. The nursery severity cap is gone; a nursery
stream no longer reaches that code.

## Consequences

- Alerts now rest only on streams whose surprise is known to mean what it says.
  Run on the archive (14 days to 5 Oct, 82 streams), the audit promotes 10:
  the four earthquake counts (USGS, EMSC, M4.5+), the prober, IODA outage
  events, NWS severe warnings, GDELT (context, never evidence), the German
  radiation network (BfS) and GB grid frequency. Of the 72 left: 38 have the
  wrong shape (mostly the continuous streams' fixed observation scale: every
  Cloudflare Radar country, markets, rivers, RIPE Atlas, FIRMS), 6 a tail fault
  alone (overconfident `swpc_kp`, `swpc_xray`, `adsb_gnss_interference` among
  the shape faults; underconfident rare-event counts such as `nws_extreme`,
  `usgs_significant`, `sigmet_volcanic_ash`), and 28 too few PITs yet (the new
  satellite streams among them). Those stop contributing until their models
  are fixed, which is now visible work: `/api/nursery` says why each one waits.
  Corroborated alerts therefore need two of: the earthquake counts, BfS
  radiation, grid frequency (physical); the prober, IODA (infrastructural).
  Every-event alerts from authoritative feeds are unchanged.
- The audit is one table scan grouped by (stream, window, decile, tail):
  24 s over the VPS's 14 days of surprise rows, every 6 hours, niced.
- A new source needs about 3 days and 200 PITs before it can count.
- The criteria are effect size plus significance, so they neither reject every
  large stream nor pass noise; they are constants in the module, revisable
  with the archive as evidence.
- The pollers' conditional-request memo moves into the database
  (`poll_state`) in the same change: a restart no longer re-downloads every
  granule (~170 MB for the four night-light tiles alone, seen by the resource
  monitor on 5 Oct).
