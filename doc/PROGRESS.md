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
| 7 | Remaining pollers | ◐ partly — see below |
| 8 | Alert engine | ✅ done |
| 9 | Push + map | ⬜ next |
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
  There is also a **common-mode guard**: if lots of unrelated feeds go quiet at
  the same moment, that's the fingerprint of _our_ internet or server failing
  (nothing real takes out earthquakes, markets, and Wikipedia at once), so that
  silence is written off as our-side rather than raising a false global alarm.
  (A dedicated connectivity prober will make this attribution airtight later.)

## Step 7 so far — feeds now connected

Live and flowing end to end: **earthquakes** (USGS), **crypto markets** (BTC and
ETH), **radiation** (Safecast), and **US severe-weather alerts** (NWS). Adding
the second market (ETH) needed only a short config entry and no new code —
confirming the "a feed is a recipe, not a program" design goal. The weather
alerts needed one new small reader for that data shape, which now also covers
similar alert feeds.

The remaining core feeds are documented with exactly what each needs
(`doc/tier1-onboarding-status.md`): **world news (GDELT)** needs a bit of format
research; **Wikipedia traffic** is ready but needs a small tweak so the fetcher
fills in the date range each poll; **internet health** and **night-lights** need
you to register for a free account/API key (which stays out of the code — the
system reads it from an environment variable). Those last two are the natural
points where a bit of your input unblocks them.

- **The alert engine** turns surprise into alarms — but only when three things
  line up at once: the surprise *persists* over several time windows, it's
  *geographically coherent* (nearby areas from different feeds fall in the same
  region), and it shows up across *two or more independent kinds of feed* in the
  same place. A lone spike in a single feed never raises an alarm — it takes
  corroboration. A crash (a value dropping far below normal) counts just as much
  as a surge, and a cluster of feeds going silent together is itself an alarm.
  Each alert carries its evidence (which feeds, how surprising) for review.

### Trust level

101 automated tests, all passing, plus code-quality checks; earthquakes,
weather alerts, and Wikipedia pageviews have each run through the live pipeline.

## What's next

**Step 9, push + map:** the human-facing end. A phone notification when an alert
opens, and a simple world map showing where surprise is concentrated (with a
"silence map" toggle), so you can click through from an alarm to the evidence.
This is the last big feature before the final step — running it unattended on a
server for two weeks.

## A note on where the code lives

All of this is on a work branch called `p0-skeleton-pollers` (six commits), not
yet merged into the main line — so it's easy to review before it becomes
official.
