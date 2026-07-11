# Worldwatch — progress in plain language

_Last updated: 2026-07-11. Companion to the technical docs
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
| 7 | Remaining pollers | ✅ done — all 8 feeds live |
| 8 | Alert engine | ✅ done |
| 9 | Push + map | ✅ done |
| 10 | Run on server, soak test | ◐ scaffolding done; soak needs the server |

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

## Step 7 complete — all eight feeds connected

Live and flowing end to end: **earthquakes** (USGS), **crypto markets** (BTC and
ETH), **world news** (GDELT), **Wikipedia traffic**, **internet health**
(Cloudflare Radar — global plus UK, US, and Japan), **US severe-weather alerts**
(NWS), **radiation** (Safecast), and **satellite night lights** (NASA Black
Marble, four regions: UK/Ireland, western Europe, US east coast, Japan). Every
one has been tested against the real feed, not just in simulation.

Highlights from connecting the last four:

- **Internet health** uses the API key you registered. One wrinkle: Cloudflare
  only shares traffic numbers rescaled against the busiest moment of the
  window you ask about — so we always ask for a full week, which keeps that
  yardstick (the weekly peak) steady from poll to poll.
- **Night lights** downloads one small (~10 MB) satellite file per region per
  day using your NASA account, boils it down on the spot to a few hundred
  "average brightness of this ~50 km area" numbers, and throws the file away —
  no imagery is ever stored. One genuine discovery: above ~50° north there is
  simply no usable night-lights data around midsummer, because the sky never
  gets fully dark — so the UK region reports only in the darker months. The
  system treats that as an honest seasonal silence, not a fault.
- **World news** pulls GDELT's raw batch file every 15 minutes (about 1 MB,
  ~700 geolocated news events per batch) and keeps only "an event happened
  here, now" — headlines, actors, and links are discarded at the door.
- **Wikipedia traffic and the crypto prices** got a correctness fix along the
  way: the hourly view-counts and prices are now actually fed into the models
  on a sensible scale (before, views were silently ignored and prices would
  have looked "infinitely surprising" all the time). Both were verified live
  and with synthetic data.

- **The alert engine** turns surprise into alarms — but only when three things
  line up at once: the surprise *persists* over several time windows, it's
  *geographically coherent* (nearby areas from different feeds fall in the same
  region), and it shows up across *two or more independent kinds of feed* in the
  same place. A lone spike in a single feed never raises an alarm — it takes
  corroboration. A crash (a value dropping far below normal) counts just as much
  as a surge, and a cluster of feeds going silent together is itself an alarm.
  Each alert carries its evidence (which feeds, how surprising) for review.

- **The dashboard and push** are the human-facing end. A small web service
  serves a world map (areas shaded by how surprising they are right now), an
  alert feed you can click for the underlying evidence, and a toggle to show
  which feeds have gone silent. When an alert opens it can push a phone
  notification (via ntfy, which you can self-host); if no push channel is set up
  yet it simply records that and carries on. You can also mark an alert
  true/false from the dashboard, which feeds the accuracy scoring later.

- **The ops scaffolding** packages everything to run unattended: one command per
  job (fetch, roll-up, score, silence-check, serve), ready-made start-up scripts
  so the server keeps them running and restarts them cleanly after crashes or
  reboots, a one-shot deploy script, and a backup hook into your existing
  restic/B2. Each job talks only through the one database file, so any of them
  can restart at any moment without harm.

### Trust level

144 automated tests, all passing, plus code-quality checks. Every piece of P0
has been exercised — the packaged commands run as real processes, and the whole
chain run against live feeds into the dashboard. All eight feeds have ingested
real data through to surprise scores; earlier the full path to the map was also
proven live. (No false alarms so far: an alert needs two *different* kinds of
feed to agree on a place and time, so lone spikes stay quiet.)

## What's left for P0

The software for all ten steps is **written and verified**. What remains is not
code — it's the real-world run:

1. **You**: stand up the small server and pick a push channel (the feed
   registrations are done — remember to copy the three access keys from your
   local `.env` onto the server; see `OPERATOR-TODO.md`).
2. **Deploy**: one command (`ops/deploy.sh`) installs and starts everything.
3. **Soak**: let it run unattended for two weeks and confirm it survives crashes,
   outages, and a reboot, keeps the surprise archive filling, and visibly flags
   at least one real event (a sizeable earthquake is essentially guaranteed).

After that, P0 is done and the project moves to **P1**: the coupling graph — the
first version of learning *how the world's systems move together*, which is the
scientifically interesting payoff and the thing the attention allocator (your
earlier question) will later steer with.

## A note on where the code lives

All of this is on a work branch called `p0-skeleton-pollers` (26 commits), not
yet merged into the main line — so it's easy to review before it becomes
official.
