# Source review, 2026-09-27: what to add next

Checked on 2026-09-27, 18:30–18:50 UTC: every keyless endpoint below was fetched,
and "age" is how old its newest data point was at that moment. Where a key is
needed the official docs were read instead ("doc"). "Unverified" means neither
was possible. The catalog of categories is `global-anomaly-sources.md`; this is
the endpoint-level pass it asked for, preferring live and near-live sources.

Selection rules: free/open; live or near live; small bandwidth (the budget is
1–3 GB/day for everything); harm-relevant (ADR 0004) or a strong corroborator
of what we already have; aggregate counts only where individuals appear
(sensors at homes, aircraft, ships, probe hosts): identities and exact
positions are dropped at the door.

## Batch 1 — keyless (or the Cloudflare key we already have), add now

**Status 2026-09-27: added** (`config/sources/tier2.toml`, 42 stanzas), except
RIPEstat (IODA's BGP signal covers it; per-country calls are many) and the AWS
and GCP status pages (their regions need a place table) — both later. Added
besides: the UAE Defense Monitor's daily air-defence engagements
(`uaedefensemonitor.com/mod-reports.json`, a published dataset; unofficial,
compiled from the UAE MoD's statements; daily). Found while adding: adsb.lol
rate-limits (429 after two quick requests), so its feeds are paced 4–6 s apart;
Oman's CAP mirror is dead (last item 2023), not added.

About 0.6 GB/day in total at the cadences given.

| Source | Measures | Coverage | Age | Access | ~MB/day | Why |
|---|---|---|---|---|---|---|
| NASA FIRMS | fire hotspots, radiative power | global | 2 h 45 min | `firms.modaps.eosdis.nasa.gov/data/active_fire/noaa-21-viirs-c2/csv/J2_VIIRS_C2_Global_24h.csv` (+ NOAA-20), If-Modified-Since | 120 | fires: harm; corroborates night lights (bright), smoke, PM |
| Sensor.Community | PM2.5/PM10 (citizen) | Europe mostly, 74 countries | < 1 min | `data.sensor.community/airrohr/v1/filter/type=SDS011` (gzip), User-Agent | 80 | smoke, dust, industrial fires; ODbL |
| AirNow hourly files | PM2.5, O3, NO2, SO2, CO | US, CA, MX | 36 min | `files.airnowtech.org/airnow/today/HourlyData_YYYYMMDDHH.dat` + sites file | 40 | regulatory AQ; smoke |
| aviationweather.gov SIGMETs | volcanic ash, cyclone, storm areas | global FIRs | minutes | `aviationweather.gov/api/data/isigmet?format=json` | 16 | ash: authoritative harm (every item) |
| NOAA SWPC | Kp, X-ray flux, solar wind, alerts | global | 4–8 min | `services.swpc.noaa.gov/json/planetary_k_index_1m.json`, `/json/goes/primary/xrays-6-hour.json`, `/products/summary/solar-wind-speed.json`, `/products/alerts.json` | 10 | grid, GNSS, HF harm; continuous series |
| NOAA tsunami.gov | tsunami warnings/statements | Pacific, Atlantic, Caribbean | per event | `tsunami.gov/events/xml/PHEBAtom.xml`, `PAAQAtom.xml` | 1 | authoritative harm (every item) |
| JTWC | tropical cyclone warnings | NW Pacific, **Arabian Sea**, S. hemisphere | 2 min | `metoc.navy.mil/jtwc/rss/jtwc.rss` | 1 | Gonu, Shaheen: the Gulf of Oman's cyclones |
| NOAA NHC | active storms | Atlantic, E/C Pacific | 40 min | `nhc.noaa.gov/CurrentStorms.json` | 1 | harm; partly in GDACS |
| Gulf and region CAP feeds | official warnings | **Saudi Arabia** (live), Kuwait, Qatar, Bahrain, Iran, Pakistan, India (IMD, NDMA) | 1 min (SA, NDMA) | `ncm.gov.sa/en/cap-alerts`; `cap-sources.s3.amazonaws.com/<id>/rss.xml` (Alert-Hub mirrors); `met.gov.kw/rss_eng/kuwait_cap.xml`; `sachet.ndma.gov.in/cap_public_website/rss/rss_india.xml` | 5 | the only official warnings near Dubai: the UAE's NCM publishes no feed; one CAP parser for all |
| UK Environment Agency | river and tide levels, flood warnings | England | 23 min | `environment.data.gov.uk/flood-monitoring/data/readings?latest&parameter=level`, `/id/floods` | 8 | ~4,100 continuous gauges; OGL |
| PEGELONLINE | water levels | Germany (~740) | 1 min | `pegelonline.wsv.de/webservices/rest-api/v2/stations.json?includeTimeseries=true&includeCurrentMeasurement=true&timeseries=W` | 6 | continuous gauges; DL-DE Zero |
| Elexon Insights | GB grid frequency (15 s), fuel mix | Great Britain | 2 min | `data.elexon.co.uk/bmrs/api/v1/system/frequency?from=&to=&format=json` | 1 | frequency dips = generation trips; with night lights, outages |
| AEMO NEM | demand, price, interconnector flows | eastern Australia | 0–5 min | `visualisations.aemo.com.au/aemo/apps/api/report/ELEC_NEM_SUMMARY` | 3 | bushfires, storms, trips |
| EIA-930 | demand, generation, interchange | ~65 US balancing areas | 1.6 h | `api.eia.gov/v2/electricity/rto/region-data/data/` (DEMO_KEY; a free key is better) | 5 | US grid stress with NWS |
| OpenSky Network | aircraft per H3 cell | global (thin over oceans, Africa, Asia) | 20 s | `opensky-network.org/api/states/all` (anonymous: 400 credits/day, a global call 4) | 165 | airspace closures: conflict, ash, storms |
| adsb.lol | emergency squawks 7500/7600/7700; low-NACp share (GNSS jamming) | global feeders; hotspots | 9 s / 0.5 s | `api.adsb.lol/v2/sqk/7700` etc.; `/v2/lat/{lat}/lon/{lon}/dist/250` for Baltic, E. Med, **Gulf**, Black Sea | 20 | squawks as counts; jamming: the live form of gpsjam; ODbL |
| Digitraffic AIS | ships per cell | Baltic | 73 s | `meri.digitraffic.fi/api/ais/v1/locations` (gzip) | 13 | cable-cut, shadow-fleet area; CC BY |
| OONI | censorship tests: anomalies / measurements per country | 143 countries | 12 s (hourly bins) | `api.ooni.io/api/v1/aggregation?axis_x=probe_cc&time_grain=hour&since=&until=` | 5 | blocking; corroborates IODA, Radar; CC BY-NC-SA |
| RIPE Atlas | connected probes per country | ~15,200 probes | minutes | `atlas.ripe.net/api/v2/probes/?country_code=XX&status=1&page_size=1` (read `count`) | 15 | an independent hardware heartbeat for power/internet |
| RIPEstat | visible prefixes and ASNs per country | global | 2.5 h | `stat.ripe.net/data/country-resource-stats/data.json?resource=XX&sourceapp=worldwatch` | 3 | BGP per country without RIS Live's volume |
| Wikimedia projectviews | pageviews per language per hour | every wiki | 1.2 h | `dumps.wikimedia.org/other/pageviews/YYYY/YYYY-MM/projectviews-YYYYMMDD-HH0000` | 1 | attention by language; a language falling = a national shutdown; CC0 |
| Status pages | Cloudflare PoP status per city; AWS, GCP incidents | global | minutes | `www.cloudflarestatus.com/api/v2/components.json`, `health.aws.amazon.com/public/currentevents`, `status.cloud.google.com/incidents.json` | 30 | regional outages as items |
| Cloudflare Radar, more | traffic for AE, SA, QA, KW, BH, OM, IR, IL; `traffic_anomalies`; outage annotations; BGP hijacks/leaks | global | doc: minutes–hours | existing token; new countries are new stanzas only | 20 | the Gulf's internet; authoritative anomaly items |

Parser families needed: CAP/RSS (≈10 feeds), CSV hotspots, simple JSON values
(SWPC, Elexon, AEMO, EIA, gauges), aircraft and ship counts per cell, OONI and
Atlas counts, projectviews text, Statuspage JSON, SIGMET polygons, AirNow pipe
text: about ten parsers for ~40 stanzas.

## Batch 2 — needs a free key or account that you register

**Status 2026-09-30:** aisstream (`aisstream_chokepoints`: Hormuz, Bab-el-Mandeb
and Suez, distinct ships per half hour from their static-data broadcasts) and
the EIA key (`eia_demand` off DEMO_KEY) added; the rest not yet registered.

| Source | What it adds | How to get access |
|---|---|---|
| OpenAQ v3 | regulatory AQ outside the US/EU (Asia, Africa, LatAm, **the Gulf**) — the NO2-drop-means-shutdown case | free account at explore.openaq.org → API key; 60 req/min, 2,000/h. Exclude AirNow/EEA stations (already covered) |
| Gridradar | Continental Europe frequency, 1 s: one sensor for ~30 countries (the 2025 Iberian blackout would show first) | free registration at gridradar.net; terms: private/research use |
| ENTSO-E | load and generation per European country, cross-border flows | email transparency@entsoe.eu, subject "RESTful API access"; CC BY 4.0 |
| aisstream.io | ships at chokepoints: **Hormuz**, Bab-el-Mandeb, Suez, Malacca, Bosporus, Panama | free key; beta, 3 connections; counts only |
| OpenSky account | 4,000 credits/day (vs 400) → aircraft every 10 min | free registration (OAuth2 client credentials) |
| NASA FIRMS MAP_KEY | area queries instead of whole-world files | free, firms.modaps.eosdis.nasa.gov/api/map_key |
| EIA key | beyond DEMO_KEY's limits | free, eia.gov/opendata |
| USGS water (new API) | ~10k US gauges (the old API retires Nov 2026 – Feb 2027) | free key at api.waterdata.usgs.gov |
| Copernicus Data Space | Sentinel-5P NO2/SO2/CO per area, daily, via the Statistical API (no imagery download) | free CDSE account; 10k requests/month |

**Status 2026-10-05:** Sentinel-5P added as `s5p_no2` (29 city and industrial
boxes), `s5p_so2` (13 volcanoes, Norilsk, the Highveld) and `s5p_co` (13 fire
regions): fetcher `cdse_statistics`, parser `cdse_s5p_stats`, NRTI only, one
request per box per local day. Waiting on the CDSE OAuth client
(OPERATOR-TODO). The SO2 volcanic-layer product isn't on Sentinel Hub; only
the total column is.

**Status 2026-10-05, faster fires:** FIRMS shows geostationary fires on its map
but exports none (no CSV, no Area API). Added `goes19_fire` and `goes18_fire`:
GOES ABI FDC full disk from NOAA NODD (public, no account), the newest scan
every 20 min (~210 MB/day for both), fetcher `s3_latest`, parser `goes_fdc`
(geolocated on the 5 Oct scan: a median 1 km from a VIIRS hotspot). Next:
Meteosat via EUMETSAT LSA SAF FRP-PIXEL (MSG 0° and IODC: Europe, Africa, the
Middle East, India; HDF5 fire lists, ~20 MB/day; free account), then Himawari
(JAXA P-Tree WLF CSV; free account, FTP/SFTP only, latency unconfirmed).
Meteosat added the same day: `meteosat_fire` (MSG 0°) and `meteosat_iodc_fire`
(41.5°E), fetcher `index_latest` (public listing, Basic-auth files), parser
`lsasaf_frp_list`; built against the 5 Oct 00:00 MSG slot (125 fires; at
confidence ≥ 0.5 a median 3.4 km from a VIIRS hotspot). Measured delay: MSG
~40 min, IODC ~75 min after the slot. Himawari remains.

## Faster versions of what we have

From `source-latency.tsv`: night lights arrive ~9 days late (VNP46A2); the
near-real-time VNP46A1 via LANCE is ~3 h — the same Earthdata login. EURDEP
arrives 2–6 h late; national feeds directly are faster where they exist (BfS
already is, 1.2 h).

## Later

GTFS-RT active vehicles per city (545 keyless feeds, seconds old, one
protobuf parser; 150–300 MB/day for ~100 cities — a new dependency); RIPE RIS
Live for 3–5 peers (0.2 GB/day each; the full stream is 177 GB/day); TTN /
Packet Broker online gateways per cell (6 MB gzip per snapshot, terms to ask);
Wikimedia recent changes for ~10 wikis (the SSE stream is 5.6 GB/day
unfiltered, so poll per wiki); gpsjam.org daily (ask its author: no licence);
INCOIS Indian Ocean tsunami (the Makran zone; timed out from here, retry from
the VPS); FAA NAS status (403 from outside the US, retry from the VPS); NOAA
HMS smoke polygons; CAISO 5-min, TEPCO; WMO SWIC global CAP aggregate
(de-duplicate against NWS and MeteoAlarm); certificate-transparency volume;
Tor Metrics users per country; HANS volcano levels.

Labels and ground truth, not sensors: EM-DAT (curated, weekly, no API: a manual
download for backtests), Copernicus EMS activations (RSS), ReliefWeb (needs an
approved appname), Smithsonian GVP weekly.

## Skip

WAQI (terms forbid archiving), PurpleAir (paid at this scale),
airplanes.live (API closed) and ADS-B Exchange (paid), AISHub (needs our own
receiver), Helium (registration, not liveness), Blitzortung (no sanctioned
API), GloFAS (heavy gridded forecasts), MeshCore brokers (auth), Meshtastic
public MQTT (1.6–2.1 GB/day per region, mostly spam, and it duplicates Atlas
and IODA as a heartbeat), Downdetector (no API), raw Sentinel-5P swaths,
per-article pageview dumps (1.5 GB/day), regional seismology already inside
EMSC (GFZ, INGV, AFAD).

ShadowBroker (github.com/BigBodyCobain/Shadowbroker, AGPL-3.0) is a self-hosted
OSINT map overlaying ~60 live feeds, without calibrated detection: useful as a
catalogue (it pointed to the ADS-B NACp jamming method and Digitraffic), not
as a dependency. Much of it is our non-goals (webcams, social media, scanners).

## Licences to keep in mind

Share-alike: Sensor.Community and adsb.lol (ODbL), OONI (CC BY-NC-SA).
Non-commercial: OONI, Cloudflare Radar (CC BY-NC), OpenSky, Gridradar. Fine
for Worldwatch; relevant if derived data is published (the coupling graph).
