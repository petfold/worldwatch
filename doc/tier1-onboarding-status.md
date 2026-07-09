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
| 5 | Internet health | continuous | — | — | ⬜ needs auth (e.g. Cloudflare Radar token) |
| 6 | Severe weather (NWS) | count | `nws_severe_alerts` | `geojson_events` | ✅ live (US); zone-only alerts skipped |
| 7 | Radiation (Safecast) | continuous | `safecast_radiation` | `safecast_json` | ✅ live |
| 8 | Night lights | continuous | — | — | ⬜ needs Earthdata registration + product choice |

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

**Internet health** — the richest free option (Cloudflare Radar) needs an API
token; the operator would register personally and expose it via an env var
(the stanza references `auth_env_var`, never the key itself). Alternative free
signals (public BGP/looking-glass, our own active prober from P1) are lower
fidelity. Deferred pending a source choice.

**Night lights** — NASA Black Marble / VIIRS daily products require an Earthdata
login and a decision on which derived regional-radiance product to ingest
(raw imagery is a non-goal). Deferred pending registration + product choice.

## Notes

- Adding a source that fits an existing parser is stanza-only (guardrail 2) —
  `eth_usd` was added this way, no code. A genuinely new payload shape adds one
  parser function (`geojson_events` was added for NWS/CAP alerts).
- Auth tokens are always referenced via `auth_env_var` and set by the operator;
  keys never live in the repo (guardrail 9).
- All sources start in `nursery` status; P0 promotes to `active` manually after
  eyeballing PIT calibration (auto-promotion is P1).
