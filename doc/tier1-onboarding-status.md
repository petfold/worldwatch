# Tier-1 source onboarding status (P0)

Tracks each of the 8 Tier-1 sources from `p0-implementation-plan.md`: whether it
is live, and if not, exactly what it needs. "Live" = a config stanza exists, the
poller can fetch it, and a parser turns it into observations (verified against a
checked-in fixture and/or the real feed).

| # | Source | Flavor | Stanza | Parser | Status |
|---|--------|--------|--------|--------|--------|
| 1 | Seismic (USGS) | count | `usgs_seismic` | `geojson_features` | ✅ live (all-hour summary feed) |
| 2 | Markets/crypto | continuous | `btc_usd`, `eth_usd` | `coinbase_spot` | ✅ live (crypto); FX needs a keyed provider |
| 3 | News events (GDELT) | count | — | — | ⬜ needs endpoint research + parser |
| 4 | Wikipedia pageviews | count | `wikipedia_pageviews` | `wikimedia_pageviews` | ◐ parser ready; needs poller URL templating |
| 5 | Internet health | continuous | `cf_radar_netflows_*` | `cloudflare_radar_timeseries` | ✅ live (global + GB/US/JP; `WW_CLOUDFLARE_TOKEN`) |
| 6 | Severe weather (NWS) | count | `nws_severe_alerts` | `geojson_events` | ✅ live (US); zone-only alerts skipped |
| 7 | Radiation (Safecast) | continuous | `safecast_radiation` | `safecast_json` | ✅ live |
| 8 | Night lights | continuous | `night_lights_*` | `vnp46a2_grid` | ✅ live (4 VNP46A2 tiles; `WW_EARTHDATA_USER/PASS`) |

## What the not-yet-live sources need

**News events (GDELT)** — GDELT publishes event/GKG files every 15 min and has
the DOC/GEO 2.0 query APIs. Pick a concrete access mode (raw 15-min CSV vs a
timeline/geo query), then add a parser for it. Geocoded → count flavor per cell.
No account, but terms/rate limits apply.

**Wikipedia pageviews** — the parser (`wikimedia_pageviews`) and stanza exist and
are tested, but the endpoint is a template with `{project}/.../{start}/{end}`
date placeholders. The poller currently does a plain GET, so it needs a small
addition: per-source URL templating that fills in the rolling date range each
poll. Until then this source can't be fetched live.

## Notes on the auth'd sources (live 2026-07-11)

**Internet health (Cloudflare Radar)** — `cf_radar_netflows_{global,gb,us,jp}`
poll the netflows timeseries (15-min buckets). Radar exposes only normalized
values (`min0_max` over the queried window; no raw series on the free API), so
the stanzas use a 7d window to keep the normalization anchor — the weekly
traffic peak — stable across polls, and the parser drops the in-progress final
bucket. Country streams sit on country-centroid H3 cells (`fixed_latlon`
geocode strategy) so they join spatial coherence grouping. Adding a country is
stanza-only. Token via `auth_env_var = "WW_CLOUDFLARE_TOKEN"` (Bearer).

**Night lights (NASA Black Marble VNP46A2)** — one stanza per 10°×10° tile:
h17v03 (UK/Ireland), h18v04 (France/S. Germany/N. Italy), h10v05 (US
mid-Atlantic/SE coast), h31v05 (Japan). The `earthdata_granule` fetcher asks
CMR for a tile's newest granule and downloads it (~10 MB) only when the granule
id changes — roughly once/tile/day; the `vnp46a2_grid` parser reduces the
2400² grid in memory to per-H3-res-4-cell mean radiance (`log1p`, high-quality
pixels only, low-coverage cells dropped, never imputed) and the file is
discarded (field-drop). Product latency is 2–9 days; observations carry the
granule's own day (frozen-archive semantics accept the late fold). Known
seasonal gap: tiles above ~50°N have no high-quality retrievals near midsummer
(verified live on h17v03 in July — twilight never reaches astronomical
darkness), so h17v03 is dark-season-only; the presence channel sees the gap.
EDL token is listed/minted automatically from `WW_EARTHDATA_USER`/`PASS`
(or supplied directly via `WW_EARTHDATA_TOKEN`).

## Notes

- Adding a source that fits an existing parser is stanza-only (guardrail 2) —
  `eth_usd` was added this way, no code. A genuinely new payload shape adds one
  parser function (`geojson_events` was added for NWS/CAP alerts).
- Auth tokens are always referenced via `auth_env_var` and set by the operator;
  keys never live in the repo (guardrail 9).
- All sources start in `nursery` status; P0 promotes to `active` manually after
  eyeballing PIT calibration (auto-promotion is P1).
