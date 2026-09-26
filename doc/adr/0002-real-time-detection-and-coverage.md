# ADR 0002 — Real-time detection path, active probing, coverage balance

Date: 2026-09-26 · Status: **proposed** (for operator review)

## Context

The project's success metric is lead time over mainstream news (architecture
§13). Measured per source on the live deployment
([`doc/source-latency.tsv`](../source-latency.tsv)):

- **Source-side delays** range from seconds (Coinbase, NWS warnings) and
  minutes (USGS: 7 min median, 40 min p90) to hours (radiation relays
  1.2–2.2 h, Cloudflare Radar 1.8 h, Wikipedia 3.5 h) and days (night lights,
  9.3 d).
- **Our own pipeline adds 30–60 min** to everything except the authoritative
  feeds (ADR 0001). The causes are all design, not physics:

  | Step | Delay |
  |---|---|
  | raw rows wait for the 15-min fine window before consolidation | 15 min |
  | at that age they land in 20-min bins, which must fill | 0–20 min |
  | a bin is scored only after its end + settle | 20 min |
  | the alert engine wants ≥ 2 anomalous bins | ~20 min |
  | consolidate and detect run on separate 5-min timers | 0–10 min |

  For fast sources this is the largest avoidable delay. The first wire flash
  after a major earthquake comes 5–15 min after origin, and it is itself based
  on USGS or EMSC.
- **The same design fragments late-arriving streams.** Bins are assigned by
  the observation's *age at consolidation*, and Layer 0 models each
  (cell, scale) separately. So data arriving hours or days late splits into
  one-bin series. Live, on 2026-09-26: all 1,481 night-light series had one
  bin, and Cloudflare's history spread over 7 scales averaging 2.3 bins. The
  new radiation feeds (hour-plus relay lag) would fragment the same way.
- **Coverage is biased towards the US.** USGS reports quakes down to M1 in the
  US, but the global catalogue is complete only from about M4.5 elsewhere. NWS
  is US-only, with about 1,000 alerts a day, mostly minor. GDELT leans towards
  English-language news. The map and the alert engine inherit these densities.
- **Internet health has no fast, broad source.** Cloudflare Radar lags about
  1.8 h and we poll four locations.

The geometric cascade is an archive-compaction scheme. The error was using it
as the detection clock.

## Decision

### A. Score on arrival, at each stream's native resolution

1. **Detection moves into the poll process.** After a poller parses and
   stores observations, each new observation (new per the `seen` keys) goes
   straight to its stream's in-memory Layer-0 model, is scored, and is passed
   to the alert logic and notifier in the same event loop. Expected pipeline
   delay: milliseconds to seconds. No queues or new infrastructure: it's the
   existing asyncio process (guardrail 10, "no message queues").
2. **Native resolution by observation time.** Each stream models on one fixed
   bin width from its stanza (`[model] native_seconds`: quakes 300,
   radiation 3600, prices = per quote, …), assigned by *observation* time,
   whatever the arrival lag. One series per (stream, cell), no fragmentation.
3. **Point measurements score immediately.** A price, an hourly dose value or
   a traffic reading is complete when delivered, so it doesn't wait for a
   window to close.
4. **The archive is unchanged in purpose.** SQLite write-through of raw
   observations, bins and surprise rows. The consolidator keeps compacting old
   data into the geometric cascade on its timer, but detection no longer
   reads from it. The surprise archive stays the permanent record: one row
   per native bin, frozen once written.
5. **Crash-safety and isolation.** Model state is checkpointed every few
   minutes and on shutdown; on restart it replays observations newer than the
   checkpoint (`seen` guarantees exactly-once). A scoring fault in one stream
   is recorded as data and never stalls a poller (guardrail 3). The separate
   `detect` timer is kept as a catch-up sweep, idempotent over the same
   window (guardrail 7).

### B. Counts: multi-window scan on each arrival

On every new event in a count stream, compare the recent counts in several
windows at once (e.g. 1, 5, 15, 60 min) with each window's predictive, and
take the most extreme with a correction for testing several windows. Five
quakes in two minutes then alert on the fifth arrival. Counts only grow while
a window is open, so a partial count already in the extreme tail gives a
valid bound. The full-window score is still written when the native bin
closes, for calibration and the archive. Later: self-exciting (Hawkes) models
so aftershocks after a known mainshock are explained away.

### C. Sequential evidence instead of fixed persistence

"≥ 2 anomalous bins" becomes accumulated evidence (a CUSUM-type statistic on
each stream's surprise):

- a very strong single observation alerts at once;
- weaker ones accumulate until they cross a threshold set by the
  false-alarm budget.

This is the optimal delay/false-alarm trade-off (Lorden's bound: the minimum
average delay for a given false-alarm rate scales as log(1/rate) / KL
divergence). Near-zero delay is achievable exactly for the dramatic events;
subtle changes inherently need more observations. The combination across
streams follows the tails research agenda
([`doc/research-tails-and-dependence.md`](../research-tails-and-dependence.md));
until then the ADR 0001 rules apply.

**Confirm in space before time.** Waiting for several bins is only a stand-in
for confirmation. Independent sensors agreeing *at the same instant* give the
same confirmation without the wait. So evidence accumulates across sensors
as well as over time, and strong simultaneous agreement alerts on the first
observation:

- **Radiation:** `persist_n = 1`, at least 2 independent nearby stations.
  Set the per-station threshold p from a network false-alarm budget:
  expected false alarms per hour ≈ (number of neighbour pairs) × p².
  About 5,000 stations with ~10 neighbours each gives ~25,000 pairs, so
  p = 10⁻⁴ per reading is about one false alarm per six months; 3 stations
  allow p ≈ 10⁻³.
- **Prober:** 5 of a country's 10 targets failing in the same round has
  probability ≈ 2.5 × 10⁻⁸ under independent 1% loss, so it alerts after one
  round (~60 s).
- **Caveat (dependence):** the pair arithmetic assumes independence. Common
  causes break it: rain raises dose rates regionally; co-located detectors
  are one sensor; a shared upstream affects several probe targets. Until
  tail dependence is learned (the research agenda's extremal coefficient),
  guard against rain with three cheap checks:
  - size: rain gives +10–100%, rarely tenfold;
  - shape: washout decays with the ~hour half-life of radon progeny;
  - precipitation: BfS publishes a 15-min layer per station, which explains
    rain away.

**Averaging at the source** is the one delay we can't remove. Dose rates are
pulse counts over an interval, so short intervals carry more Poisson noise
(relative 1/√N). That matters only for small changes; a tenfold jump is
unmistakable in a 1-min value. BfS and EURDEP publish hourly means, which
costs the wait for the hour to end plus relay, and dilutes a late-hour jump.
Task: find open national feeds with 10-min values (Switzerland's NADAM
records them; others to verify) to cut radiation from about 2 h to about
10–20 min.

### D. Transport: push where offered, poll faster where not

| Source | Change | Floor after change |
|---|---|---|
| Earthquakes | add **EMSC WebSocket** (push, seconds); poll USGS every 60 s (its feeds update every minute) | ~1–3 min after origin |
| Crypto | **Coinbase WebSocket ticker** | sub-second |
| NWS | poll every 60 s; **NWWS-OI** push (XMPP) needs an operator-registered account | seconds after issue |
| everything else | cadence set by the source's own update rate | the source's own delay |

### E. Our own active prober

A fast, cheap, bias-controlled tripwire for internet reachability, under our
own control (the spec's `probe/`):

- **Targets:**
  - NTP pool country zones (`<cc>.pool.ntp.org`): volunteer servers that
    answer queries by design;
  - RIPE Atlas anchors (1,811 hosts meant to be measured; Europe-heavy);
  - later, if the operator registers for it, the USC/ISI address hitlist for
    countries with few public targets.
- **Stratified, not proportional:** a fixed quota per country (10–20), spread
  across distinct ASNs so one ISP's trouble isn't a country's. Sampling by
  address space would reproduce the US bias.
- **Location checked by physics:** a round trip from our vantage faster than
  light allows for the claimed distance rejects the target's geolocation.
- **Protocol:**
  - NTP queries (UDP, unprivileged, asyncio) to pool servers, at ≥ 64 s per
    server as the pool asks;
  - ICMP echo to anchors via unprivileged ICMP datagram sockets, once the
    operator allows the worldwatch group (`net.ipv4.ping_group_range`);
  - TCP connect as the fallback.
- **Cost:** about 2,000–4,000 targets on 60-s rounds = 35–70 probes/s, about
  0.5–1 GB/day of tiny packets. CPU is negligible in asyncio, with no process
  spawn per probe. The target count is tunable.
- **Signals per country (and per ASN):** fraction reachable (loss) and median
  RTT, modelled like any stream.
- **Latency:** an outage shows after a couple of missed rounds across several
  targets, so about 2–5 min.
- **Attribution, since there is one vantage point:**
  - everything failing at once is *our* network (the spec's
    `{poller-fault | network-fault}`), recorded as self-instrumentation, never
    an alert;
  - a second cheap VPS on another continent later gives a second viewpoint;
  - hosting a RIPE Atlas probe (operator account) earns credits for
    measurements from thousands of vantages.
- **Transparency:** a page on the VPS describing the measurements, with a
  contact address, as is customary for measurement research. Low rates, with
  an opt-out honoured.

### F. Aggregators cover what our sample misses

- **IODA** (Georgia Tech): active probing of every responsive /24 block,
  plus BGP and a network telescope, per country and ASN, with a free public
  API. Newest values measured at 5–15 min old for probing and about 20 min
  for BGP. It sees regional and ASN outages our sparse sample can miss.
  - Per-country IODA signals as streams (one stanza, a country list, a small
    parser).
  - IODA's own outage events as an authoritative `every_event` feed
    (ADR 0001).
- Our prober is the fast tripwire; IODA is the breadth. Agreement between them
  is corroboration by a different method.
- **Cloudflare Radar** stays as a slow traffic-volume layer (it's valuable for
  Layer-1 coupling research), not a fast detector.

### G. Coverage balance

Principle: **each region gets comparable weight, not weight proportional to
how much data happens to exist there.** Dense sources are thinned or
coarsened; sparse regions get additional sources; gaps are shown, not
hidden.

- **NWS:** fetch only `severity=Severe,Extreme` (drops the bulk of minor
  advisories) and coarsen to H3 resolution 2 (fewer, larger US cells). A few
  samples are enough. `nws_extreme` stays authoritative, at priority 4
  (silent at night).
- **USGS:** the detection stream uses a uniform completeness magnitude (M4.5+
  globally, or per-region completeness where known). M1+ quakes remain as
  evidence-store context for local detail.
- **Add non-US equivalents:** MeteoAlarm (38 European countries, open CAP
  feeds), GDACS (global disaster alerts, authoritative), EMSC (Europe and the
  Mediterranean, also faster per D).
- **Map and engine:** a per-stream spatial budget (a quota per region)
  applies to what is drawn and to how many cells a stream can contribute to
  one region's evidence. Cells with too few sources are shown as
  "unmonitored", never as quiet (the spec's coverage floor).

### H. Source changes that follow

- **Retire Safecast.** Its newest measurement is from January 2026;
  EURDEP/BfS supersede it.
- **Night lights → VNP46A1 NRT** (LANCE, about 3 h) instead of the 9-day
  gap-filled VNP46A2.
- **Deploy the radiation feeds after A**, so their hourly values are modelled
  at native resolution from the start (coarse bins can't be split later).

## Consequences

Expected event → alert, after A–G:

| Source | Now | After |
|---|---|---|
| USGS / EMSC earthquakes | 40–60 min | **~1–5 min** |
| NWS warnings | 40–60 min | **~1–2 min** (seconds with NWWS-OI) |
| Crypto moves | 35–50 min | **seconds** |
| Internet outage (own prober) | ~2.5 h (Cloudflare) | **~2–5 min** |
| Internet outage (IODA breadth) | — | ~10–25 min |
| Radiation (BfS / EURDEP, hourly) | ~2.5–3 h | ~1.2–2.2 h after the hour (the source's floor) |
| Radiation (open 10-min national feeds, if found) | — | ~10–20 min |

- The largest change is to the poller process, which now also hosts scoring
  and the alert fast path. Failure isolation and exactly-once scoring are
  requirements, with tests.
- Nothing about calibration changes: q_values are still PITs under Layer-0
  predictives, now at a consistent native resolution.
- Faster also means more exposure to false alarms. The sequential-evidence
  thresholds (C) and the ADR 0001 confirmation rules are the counterweight.

**Operator items:**

- allow unprivileged ICMP for the worldwatch group (sysctl), optional;
- NWWS-OI account, optional;
- RIPE Atlas account / probe, optional;
- a second VPS, optional.

## Phasing

1. **A + C core:** in-process scoring at native resolution, checkpoints and
   replay, sequential evidence. This also fixes fragmentation.
2. **D:** EMSC and Coinbase WebSockets, faster USGS/NWS polling.
3. **E + F:** own prober (NTP + anchors, stratified); IODA signals and events.
4. **G + H:** coverage balance, MeteoAlarm, GDACS, Safecast retired, night
   lights NRT, then the radiation deploy.
