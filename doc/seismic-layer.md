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
public and MEMS sensitivity rules out distant events.

## Plan

1. **Discrimination enrichment (small; context only).** For M ≥ 4 events,
   fetch the USGS/EMSC event details: the event type label; the magnitude set
   (mb, Ms, Mw), giving the mb : Ms discriminant; depth with its uncertainty,
   flagged when it is the fixed 10 km default; distance to known test sites.
   Goes to the evidence store and push text, e.g. *"M5.1 shallow,
   mb–Ms explosion-like, 15 km from Punggye-ri"*.
2. **Event-triggered waveform screening.** For shallow events in unusual
   places, fetch minutes of waveforms from GSN, GEOFON and nearby Raspberry
   Shakes, then:
   - pick P onsets and first-motion polarity with pretrained models
     (SeisBench: PhaseNet-type pickers, polarity classifiers). "Compressional
     at every azimuth" is explosion-like;
   - regional high-frequency P/S (e.g. Pn/Lg) spectral ratios;
   - mb : Ms once surface waves arrive (20–40 min);
   - output a *screening score*, not a verdict (mining blasts and small
     events stay hard; full isotropic moment-tensor inversion is left out).

   New dependencies: ObsPy (instrument response, processing) and SeisBench,
   justified per guardrail 10. First screening ≈ 10–15 min after origin
   (P arrival + picking); mb : Ms ≈ 20–40 min.
3. **Raspberry Shake urban-explosion detector.** Continuous streams from dense
   city clusters; impulsive, shallow, local signals seen by several nearby
   units at once ("confirm in space before time", ADR 0002). The independent
   seismic counterpart to radiation and news for explosions and industrial
   accidents (Beirut 2020 was recorded clearly by nearby Raspberry Shakes).
