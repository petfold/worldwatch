# ADR 0001 — Per-source alert policy

Date: 2026-09-26 · Status: accepted

## Context

The spec (architecture §8) opens an alert only when persistence × geographic
coherence × ≥2 independent modalities agree. That rule is a false-alarm filter:
one stream alone can misfire through noise, a faulty sensor, or
miscalibration. Two kinds of source break the assumption behind it:

- **Unique sensors.** A radiation network measures something no other source
  sees, and nothing will confirm it for hours. Waiting for a second modality
  throws away the lead time the system exists to win.
- **News.** GDELT is the news, published 15–60 min after the articles it reads.
  It cannot be faster than the news, so counting it as independent
  confirmation of a sensor anomaly rewards being late. It is valuable as
  context ("what is this?") and as the lead-time yardstick (§13), not as
  evidence.
- **Authoritative feeds** (USGS significant earthquakes, NWS Extreme alerts)
  have already judged significance; modelling their counts adds latency and no
  information.

## Decision

A stanza's optional `[alerts]` table sets its policy; with none, a stream
corroborates as before.

| Setting | Meaning |
|---|---|
| `role = "context"` | never counts toward corroboration; pushes show it as "news in the area (context, not evidence)" |
| `single_source = true` | may alert alone, confirmed within its own network: `min_sensors` distinct cells in one region (`region_resolution`), each past the stricter `q_tail` for `persist_n` bins |
| `every_event = true` | each newly ingested item is an alert (`severity`), read from the ingestion keys (`seen`) so it fires within one detect pass, without waiting for bins to close |

Safeguards:

- A single faulty detector can never pass `min_sensors`.
- Single-source streams still in the nursery (not yet shown calibrated) are
  capped at severity 0.85 → priority 4: visible, never waking.
- The engine still reads only the interchange contract plus ingestion keys;
  no raw values reach it.

## Consequences

- GDELT (`role = "context"`) no longer corroborates; it counts distinct
  articles, not coded events.
- EURDEP and German ODL radiation are single-source (≥3 stations, 1-in-10⁶,
  2 bins). Known confounder: rain raises dose rates regionally; until
  precipitation explains it away (P1), thresholds stay strict.
- USGS significant earthquakes (severity 0.95, wakes) and NWS Extreme
  (severity 0.8, silent at night) are `every_event`.
- Remaining latency limit for everything else: bins are scored only once
  closed (≈ 40–60 min after an observation at the current fine window); to be
  addressed separately.
