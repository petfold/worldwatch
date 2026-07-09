# Worldwatch — progress in plain language

_Last updated: 2026-07-10. Companion to the technical docs
(`worldwatch-architecture-v0.1.md`, `p0-implementation-plan.md`)._

## What we're building, in one paragraph

Worldwatch watches lots of free public data feeds at once — earthquakes,
markets, news, Wikipedia traffic, radiation sensors, and more — and learns what
"normal" looks like for each one. When something drifts far enough from normal,
in a calibrated way, and several unrelated feeds agree that something is
happening in the same place at the same time, it raises an alert. The hope is to
notice big events a little _before_ the mainstream news does. A second payoff is
the map of _how the feeds relate to each other_ that falls out of the learning.

## How the system is shaped

Think of it as an assembly line. Data flows left to right:

```
feeds → pollers → raw store → consolidator → per-feed models → surprise archive → alerts → map/phone
```

1. **Pollers** fetch each feed on a schedule and hand back tidy records.
2. **Raw store** keeps the most recent fine-grained data briefly.
3. **Consolidator** rolls older data into coarser and coarser time buckets, so
   recent history is detailed and ancient history is summarised (cheap to keep
   forever).
4. **Per-feed models** learn each feed's normal rhythm (daily/weekly cycles,
   typical noise) and, for every new data point, output one number: _how
   surprising was this?_ — on a clean 0-to-1 scale.
5. **Surprise archive** is the permanent memory: every "how surprising" score,
   forever, but tiny (megabytes per year).
6. **Alerts** (later) fire only when surprise persists, is geographically
   coherent, and shows up across several independent kinds of feed.
7. **Map / phone** (later) shows it all and pushes notifications.

The golden rule: higher layers only ever see the _surprise scores_, never the
raw data. That one clean interface is what lets very different feeds be compared
fairly.

## The plan (P0 = the first working version)

P0 is the first milestone: run 8 core feeds through the whole line unattended on
a cheap server, fill the surprise archive, and push a basic alert to a phone.
The build order is ten steps:

1. Project skeleton + database
2. First two pollers working end to end
3. Consolidator (the time-bucket roll-up)
4. Model for continuous feeds (prices, radiation levels…)
5. Model for count feeds (number of earthquakes, news events…)
6. Presence channel — treat "a feed went silent" as its own signal
7. The remaining pollers
8. Alert engine (a simple first version)
9. Push notifications + web map
10. Operations: run it on the server, back it up, let it run 14 days

## Where we are now

**Steps 1–5 plus the "runner" that connects them are done.** The whole
left-to-right line works end to end — we tested it on the live US Geological
Survey earthquake feed and watched real earthquakes flow all the way into the
surprise archive with sensible scores.

| Step | Plain description | Status |
|------|-------------------|--------|
| 1 | Skeleton + database | ✅ done |
| 2 | First pollers (earthquakes, Wikipedia) | ✅ done |
| 3 | Consolidator | ✅ done |
| 4 | Continuous-feed model | ✅ done |
| 5 | Count-feed model | ✅ done |
| — | Runner (glues models to the data) | ✅ done |
| 6 | Silence-as-a-signal | ✅ done |
| 7 | Remaining pollers | ⬜ next |
| 8 | Alert engine | ⬜ |
| 9 | Push + map | ⬜ |
| 10 | Run on server, soak test | ⬜ |

### What each finished piece actually does

- **Pollers** fetch politely (they identify themselves, respect "nothing
  changed" responses, back off when a feed is down) and — importantly — a
  broken feed can never stall the others. Every fetch, success or failure, is
  recorded as data, so we can always tell "the world went quiet" apart from
  "our system broke."
- **Consolidator** buckets data into a "detailed now, blurry past" timeline and
  can be safely re-run after a crash without double-counting.
- **The two models** are the mathematical heart. Each learns a feed's normal
  daily/weekly pattern online (no heavy training) and is deliberately robust: a
  single wild spike barely budges its sense of normal. We proved with synthetic
  data that their surprise scores are _honest_ — a "5% surprising" event really
  does happen about 5% of the time — and that they correctly flag genuine
  bursts.
- **The runner** keeps one small model per feed-per-place, remembers each
  model's state between runs, and only scores new data — so it's cheap and
  restart-safe.
- **The presence channel** treats a feed _falling silent_ as its own signal. It
  learns each feed's normal rhythm, so a sensor going quiet during its usual
  nightly maintenance window raises nothing, but a normally-reliable feed going
  unexpectedly dark gets flagged — and the longer the silence, the louder. It is
  careful to tell "the feed went quiet" apart from "our own poller wasn't
  running", so the system never mistakes its own downtime for a world event.

### Trust level

Everything above has automated tests (81 of them, all passing) plus code-quality
checks, and the earthquake pipeline has been run against the real live feed. So
this isn't just written — it's verified working.

## What's next

**Step 7, the remaining feeds:** wire up the other core sources (world news,
internet health, severe-weather alerts, night-lights, and so on). Because adding
a feed is meant to be "write a short config entry, not new code", this step is
mostly breadth — pointing the existing machinery at more of the world.

## A note on where the code lives

All of this is on a work branch called `p0-skeleton-pollers` (six commits), not
yet merged into the main line — so it's easy to review before it becomes
official.
