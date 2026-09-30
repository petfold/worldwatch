"""Batch 1 of the source review: each new stanza parses a real (trimmed) payload."""

import json
import pathlib

import h3
import pytest

from worldwatch.ingest.parsers import parse

FIX = pathlib.Path(__file__).parent / "fixtures"


def _text(name):
    return (FIX / name).read_text()


def _json(name):
    return json.loads(_text(name))


def _obs(sources, sid, payload):
    return parse(payload, sources[sid])


def test_cap_alerts_place_each_warning_where_its_polygon_is(sources):
    obs = _obs(sources, "cap_saudi", [{"url": "u", "text": _text("cap_ncm_alert.xml")}])
    assert len(obs) == 3  # one warning for three governorates, each in its own cell
    for o in obs:
        lat, lon = h3.cell_to_latlng(o.cell)
        assert 18 < lat < 23 and 38 < lon < 43  # Makkah region
        assert o.value == 3.0  # Severe
    assert obs[0].context["event"] == "Moderate rains" and obs[0].context["severity"] == "Severe"
    assert len({o.ts for o in obs}) == 3


def test_cap_without_coordinates_falls_back_to_the_country(sources):
    cap = _text("cap_ncm_alert.xml")
    import re

    cap = re.sub(r"<polygon>.*?</polygon>", "", cap, flags=re.S)
    (o,) = _obs(sources, "cap_saudi", [{"url": "u", "text": cap}])
    from worldwatch.config.countries import country_cell

    assert o.cell == country_cell("SA", 3)


def test_linked_get_fetches_each_listed_document_once(sources):
    import asyncio

    import httpx

    from worldwatch.poll.fetch import _linked_seen, get_fetcher
    from worldwatch.poll.http import CacheValidators

    listing, doc = _text("cap_ncm_listing.xml"), _text("cap_ncm_alert.xml")
    got = []

    def handler(req):
        got.append(str(req.url))
        return httpx.Response(200, text=listing if "cap-alerts" in str(req.url) else doc)

    cfg = sources["cap_saudi"]
    _linked_seen.pop(cfg.stream_id, None)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            a = await get_fetcher(cfg)(c, cfg, CacheValidators(), 0)
            b = await get_fetcher(cfg)(c, cfg, CacheValidators(), 0)
        return a, b

    a, b = asyncio.run(run())
    assert len(a.payload) == 3 and b.payload == []  # the second poll fetches only the listing
    assert sum("cap/en/alerts" in u for u in got) == 3


def test_jtwc_warning_position_and_wind(sources):
    (o,) = _obs(sources, "jtwc_cyclones", [{"url": "u", "text": _text("jtwc_warning.txt")}])
    lat, lon = h3.cell_to_latlng(o.cell)
    assert abs(lat - 25.9) < 1 and abs(lon - 129.1) < 1
    assert o.value == 125.0 and o.context["name"].startswith("Typhoon 25W")


def test_tsunami_bulletins_below_the_level_are_dropped(sources):
    payload = {"url": "u", "text": _text("tsunami_atom.xml")}
    assert _obs(sources, "tsunami_ptwc", payload) == []  # an information statement
    import dataclasses

    cfg = dataclasses.replace(sources["tsunami_ptwc"], parse={"format": "tsunami_atom", "min_level": 0})
    (o,) = parse(payload, cfg)
    assert o.value == 0.0 and o.context["region"] == "Loyalty Islands" and o.context["magnitude"] == 7.0


def test_nhc_storms(sources):
    obs = _obs(sources, "nhc_storms", _json("nhc_storms.json"))
    assert len(obs) == 2 and obs[0].value == 30.0 and obs[0].context["name"] == "Fay"


@pytest.mark.parametrize("sid,fixture,n", [("swpc_kp", "swpc_kp.json", 5), ("swpc_xray", "swpc_xray.json", 3)])
def test_space_weather_is_its_own_place(sources, sid, fixture, n):
    obs = _obs(sources, sid, _json(fixture))
    assert len(obs) == n and {o.cell for o in obs} == {"SPACE"}
    assert len({o.ts for o in obs}) == n


def test_volcanic_ash_sigmets_only(sources):
    obs = _obs(sources, "sigmet_volcanic_ash", _json("isigmet.json"))
    assert len(obs) == 2 and all(h3.is_valid_cell(o.cell) for o in obs)


def test_firms_hotspots(sources):
    obs = _obs(sources, "firms_viirs_noaa21", {"url": "u", "text": _text("firms_viirs.csv")})
    assert len(obs) == 5 and obs[0].value == 2.31
    assert len({(o.cell, o.ts) for o in obs}) == 5  # hotspots of one pass stay distinct


def test_sensor_community_median_per_area(sources):
    (o,) = _obs(sources, "sensor_community_pm25", _json("sensor_community.json"))
    import math

    assert math.expm1(o.value) == pytest.approx(7.5)  # the median of 5..10
    assert h3.get_resolution(o.cell) == 4


def test_airnow_pm25_per_monitor(sources):
    obs = _obs(sources, "airnow_pm25", {"url": "u", "text": _text("airnow_aqobs.csv")})
    assert obs and all(h3.get_resolution(o.cell) == 7 for o in obs)
    import math

    assert any(math.expm1(o.value) == pytest.approx(4.2) for o in obs)  # Charlottetown


def test_pegelonline_levels(sources):
    obs = _obs(sources, "pegelonline_water", _json("pegelonline.json"))
    assert len(obs) == 3 and obs[0].value == 113.0 and obs[0].context["water.shortname"] == "ALLER"


def test_ea_flood_warnings_keep_warnings_not_alerts(sources):
    obs = _obs(sources, "ea_flood_warnings", _json("ea_floods.json"))
    items = _json("ea_floods.json")["items"]
    assert len(obs) == sum(i["severityLevel"] in (1, 2) for i in items)


def test_grid_feeds(sources):
    assert [o.value for o in _obs(sources, "elexon_frequency", _json("elexon_frequency.json"))][:2] == [49.951, 49.953]
    nem = _obs(sources, "aemo_nem_demand", _json("aemo_nem.json"))
    assert len(nem) == 5 and len({o.cell for o in nem}) == 5
    eia = _obs(sources, "eia_demand", _json("eia_region.json"))
    assert all(h3.is_valid_cell(o.cell) for o in eia)


def test_aircraft_per_area_and_gnss_interference(sources):
    import dataclasses

    cfg = sources["opensky_aircraft"]
    cfg = dataclasses.replace(cfg, parse={**cfg.parse, "min_records": 1})  # six aircraft in the fixture
    obs = parse(_json("opensky_states.json"), cfg)
    assert obs and all(o.ts % 900 == 0 for o in obs)
    assert sum(round(__import__("math").expm1(o.value)) for o in obs) == sum(
        not s[8] for s in _json("opensky_states.json")["states"])
    frac = _obs(sources, "adsb_gnss_interference", [{"target": "t", "cc": "LV", "text": _text("adsb_point.json")}])
    assert all(0 <= o.value <= 1 for o in frac)


def test_emergency_squawks_keep_no_identity(sources):
    (o,) = _obs(sources, "adsb_emergency_squawks", [{"target": "7700", "cc": "", "text": _text("adsb_squawk_7700.json")}])
    assert o.value == 7700.0 and "hex" not in (o.context or {}) and "x1" not in json.dumps(o.context)


def test_ships_per_area(sources):
    obs = _obs(sources, "digitraffic_ships", _json("digitraffic_ais.json"))
    assert obs and sum(round(__import__("math").expm1(o.value)) for o in obs) == 5


def test_ooni_share_blocked_skips_the_filling_hour(sources):
    payload = _json("ooni_aggregation.json")
    obs = _obs(sources, "ooni_anomalies", payload)
    newest = max(r["measurement_start_day"] for r in payload["result"])
    assert obs and all(0 <= o.value <= 1 for o in obs)
    from worldwatch.ingest.generic import parse_time

    assert all(o.ts < parse_time(newest) for o in obs)


def test_atlas_probes_per_country(sources):
    import math

    obs = _obs(sources, "ripe_atlas_probes", _json("atlas_probes.json"))
    assert sum(round(math.expm1(o.value)) for o in obs) == 6  # every probe counted in its country


def test_cloudflare_pops_per_country(sources):
    obs = _obs(sources, "cloudflare_pops", _json("cloudflare_components.json"))
    comps = [c for c in _json("cloudflare_components.json")["components"] if c["status"] != "operational"]
    assert 0 < len(obs) <= len(comps)


def test_wikipedia_views_per_language(sources):
    obs = _obs(sources, "wikipedia_languages",
               {"url": ".../projectviews-20260927-160000", "text": _text("projectviews-20260927-160000")})
    cells = {o.cell for o in obs}
    assert {"wikipedia:ar", "wikipedia:fa", "wikipedia:uk"} <= cells and "wikipedia:en" not in cells


def test_uae_air_defence_days_with_engagements(sources):
    rows = _json("uae_mod_reports.json")
    obs = _obs(sources, "uae_air_defence", rows)
    days = [r for r in rows if (r["daily_uavs_engaged"] or 0) + (r["daily_cruise_missiles_engaged"] or 0)
            + (r["daily_ballistic_missiles_engaged"] or 0) > 0]
    assert len(obs) == len(days) and obs[0].value == 40.0
    assert obs[0].ts == 1775001600 - 4 * 3600  # 2026-04-01 00:00 in the UAE


def test_fx_rates_one_series_per_watched_currency(sources):
    """The fixture follows the documented latest.json shape, with the rates of the first
    real poll (2026-09-30 05:00 UTC) for the watched currencies."""
    import math

    from worldwatch.config.countries import country_cell

    payload = _json("oxr_latest.json")
    by = {o.context["key"]: o for o in _obs(sources, "fx_usd", payload)}
    assert set(by) == {"AED", "ARS", "EGP", "EUR", "GBP", "IRR", "JPY", "SAR", "TRY"}
    assert {o.ts for o in by.values()} == {payload["timestamp"]}
    assert by["EUR"].value == pytest.approx(math.log(0.8825))
    assert by["IRR"].cell == country_cell("IR", 3) and by["EUR"].cell == country_cell("DE", 3)


def test_every_new_stanza_has_a_display_and_a_tail_or_reach(sources):
    import tomllib

    new = tomllib.loads((pathlib.Path(__file__).parents[1] / "src/worldwatch/config/sources/tier2.toml").read_text())
    for sid in new:
        cfg = sources[sid]
        assert cfg.extra.get("display", {}).get("label") and cfg.extra.get("display", {}).get("about"), sid
        assert "reach_km" in cfg.extra.get("alerts", {}) or cfg.modality == "informational", sid
