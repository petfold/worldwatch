# Seismic layer — beyond the USGS catalogue

Status: **planned** (after ADR 0002 phases 2–4). Recorded 2026-09-26 from
discussion with the operator.

## Why

The USGS summary feed gives location, depth, magnitude and (after analyst
review, often hours later) an event type. It carries no waveforms and no
directional information. To tell explosions from earthquakes early, and to
see local explosions and industrial accidents in cities, we need the raw data
and a few standard discriminants.

## Data (measured 2026-09-26)

| Source | Open stations live | Latency | Access |
|---|---|---|---|
| EarthScope (formerly IRIS) | 5,309 broadband, 217 networks; includes the 120-station Global Seismographic Network | newest sample 20–30 s old via the web service | FDSN web services, SeedLink |
| GEOFON (GFZ) | 492, 42 networks (Europe, Africa/Middle East, Asia) | near real time | FDSN, SeedLink |
| ORFEUS / EIDA | thousands more in Europe (not yet counted) | near real time | FDSN |
| Raspberry Shake (hobbyist) | 6,208 online (Americas 2,918, Europe 1,918, Asia 842, Oceania 300, Africa/Middle East 168) | near real time | FDSN, CC-BY; mostly 4.5 Hz geophones in homes |

Real-time *products* worth reusing as context: USGS automatic locations
(minutes), EMSC detections and felt reports (WebSocket), GEOFON automatic
moment tensors. CTBTO's bulletins do exactly this discrimination but are not
public. Phone-accelerometer networks (Google's Android system, MyShake,
Earthquake Network) detect strong local shaking fast, but their raw data is not
public and MEMS sensitivity rules out distant events; not used.

## Principles (operator, 2026-09-26)

- **Long-range effects only.** No phone-accelerometer data: phones see strong
  local shaking, not the teleseismic signals discrimination needs.
- **Alert-triggered, not sampled.** Continuously processing a sample of
  stations would likely miss the interesting moments and exceed the VPS's
  processing budget. Networks that already detect events tell us *when and
  where*; the data centres archive every waveform, so FDSN `dataselect` lets
  us fetch exactly that window after the fact (20–30 s after real time at the
  earliest) and analyse only it.

## Triggers

| Trigger | How it arrives | Window to fetch |
|---|---|---|
| USGS catalogue events (already ingested) | feeds updated every minute | origin −1 min … +15 min (P), … +40 min (surface waves) |
| EMSC events | WebSocket push (ADR 0002 §D) | same |
| GEOFON automatic solutions | FDSN event service | same |
| Our own alerts (radiation, internet outage, …) | the alert engine | the alert's region and time, looking for an explosion signature before it |

Filter before fetching: magnitude ≥ 4 or an own alert; shallow or depth
unconstrained; away from known seismic zones or near known test sites. About
40 M4+ events a day worldwide; each analysis is a few MB and seconds of CPU.

## Plan

1. **Discrimination enrichment (small; context only).** For each triggered
   event, fetch the USGS/EMSC event details: the event type label; the
   magnitude set (mb, Ms, Mw), giving the mb : Ms discriminant; depth with its
   uncertainty, flagged when it is the fixed 10 km default; distance to known
   test sites. Goes to the evidence store and push text, e.g. *"M5.1 shallow,
   mb–Ms explosion-like, 15 km from Punggye-ri"*.
2. **Triggered waveform screening.** For each triggered window, choose the
   ~10–20 stations best spread in azimuth and distance (GSN, GEOFON, EIDA,
   nearby Raspberry Shakes), fetch the window, then:
   - pick P onsets and first-motion polarity with pretrained models
     (SeisBench: PhaseNet-type pickers, polarity classifiers). "Compressional
     at every azimuth" is explosion-like;
   - regional high-frequency P/S (e.g. Pn/Lg) spectral ratios;
   - mb : Ms once surface waves arrive (20–40 min);
   - output a *screening score*, not a verdict (mining blasts and small
     events stay hard; full isotropic moment-tensor inversion is left out).

   New dependencies: ObsPy and SeisBench, justified per guardrail 10. First
   screening ≈ 10–15 min after origin; mb : Ms ≈ 20–40 min.
3. **Retrospective check for own alerts.** When Worldwatch alerts on
   something non-seismic (radiation, outage), fetch the region's seismometers,
   including dense Raspberry Shake clusters in cities, over the hours before
   it, and look for impulsive, shallow, local signals seen by several nearby
   units at once ("confirm in space before time", ADR 0002). This replaces an
   always-on urban detector (Beirut 2020 was recorded clearly by nearby
   Raspberry Shakes).
