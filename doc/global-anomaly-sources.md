# Global Anomaly Detection System — Data Source Catalog
Status: category level, agreed 2026-07-05. Individual endpoint URLs to be added per source (see "Next steps").
Constraint: free / open sources only; sampled coverage acceptable; budget ~1–3 GB/day ingest.

## Tier 1 — Phase 0 core (SHTF-weighted)
Spans physical / economic / infrastructural / informational so cross-corroboration works from day one.

1. Seismic & hazard feeds (earthquakes, volcanoes, tsunamis) — minutes latency; doubles as ground truth for alert testing
2. Markets, FX, crypto — fastest economic sensor
3. Machine-coded global news events (GDELT-style) — ~15 min cadence, geocoded
4. Wikipedia pageviews + edit spikes — global attention sensor, hourly
5. Internet health — BOTH layers:
   - Infrastructure: BGP, country/region traffic volumes, DNS, outage detection
   - Service: radar-style per-service traffic trends, provider status pages, CT logs
   - Plus: own active prober on VPS (HTTP/DNS checks against major services, every few min)
6. Severe weather alerts (structured, geocoded)
7. Radiation monitoring — government networks (EU exchange platform, national) + citizen sensor networks (Safecast-style); gamma dose rates, near-real-time, geocoded
8. Satellite night lights radiance — daily global; power outages, blackouts, regions going dark

## Tier 2 — immediate additions
9. Flight density (crowdsourced ADS-B, sampled free tiers) — also extract emergency squawk codes + military/medevac activity patterns
10. Energy grid — load, generation mix, cross-border flows, prices, grid FREQUENCY deviations (Europe best: transparency platform; gas network flows too; US coarser)
11. GTFS-RT city transit heartbeats — one number per city per bin: active vehicles vs seasonal norm; ~50 cities via public feed registries, single parser
12. Satellite trace gases — NO2 (industrial/traffic activity proxy), SO2 (volcanic/industrial), CO, methane (pipeline damage)
13. Satellite fire hotspots (near-real-time)
14. Air quality sensor networks — fires, industrial accidents, conflict
15. CAP-format official emergency alert feeds (many countries) + global disaster coordination feeds (GDACS-style)
16. Search trends (rate-limited, fragile free access)
17. Conflict/protest event databases (daily–weekly latency; use as corroboration + training labels)
18. Water: river gauges, reservoir levels (US/EU hydrological agencies), satellite altimetry for large reservoirs. NOTE: municipal drinking-water status not published anywhere real-time — known blind spot; proxies = reservoirs, drought indices, emergency alerts
19. More satellite products (low-bandwidth derived feeds, not raw imagery):
    - Aerosols/smoke, soil moisture & drought indices, snow/ice/flood extent, sea surface temp, vegetation health (NDVI, famine early warning)
    - Rule: ingest regional aggregates/anomaly products; pull imagery tiles only on-demand AFTER an alert fires
20. Public-safety radio activity levels (aggregator streams, US-centric) — channel busyness vs normal, not content

## Tier 3 — deferred
- Sampled AIS ship tracking (few genuinely free sources)
- Social media firehoses (access shrinking)
- Raw satellite imagery (bandwidth/compute; on-demand only)
- Per-city one-off transit integrations beyond GTFS-RT
- Border/migration stats, corporate filings, job postings, app rankings, domain registrations, space weather, wastewater, excess mortality, sanctions/legislative feeds, election calendars, humanitarian data exchanges, bike-share feeds, maritime nav warnings, NOTAMs (NOTAMs may promote alongside ADS-B)

## Design principles agreed
- Poll aggregated endpoints, never raw firehoses; compress; delta-fetch; drop fields at ingestion
- Multi-scale geometric time bins: bin width proportional to age (~8 bins per doubling), consolidation cascade (minute→5min→hourly→daily→weekly), keep min/max/count/variance per bin, not just mean
- FEED SILENCE IS A SIGNAL: "station stopped reporting" is its own anomaly type; regional clusters of silence = strong alert
- Alert escalation requires: persistence over N windows + geographic coherence + cross-source corroboration
- Split deployment: small VPS for 24/7 ingestion + consolidated store; local machine syncs store (MB/day) for modeling/backtests; degrade to "last known picture + silence map" if feeds die
- Ground truth for evaluation: Wikipedia Current Events / GDELT records, replayed historically; score on lead time

## Next steps
- [ ] Per-source endpoint URLs, auth requirements, rate limits, poll cadence (city-level lists for GTFS-RT, radiation stations, grid operators)
- [ ] Common record schema: (stream, cell, scale, bin) + statistics + silence representation
- [ ] Architecture doc: process layout, storage, UI
