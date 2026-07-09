"""Parser stability against checked-in golden fixtures."""

from conftest import load_fixture
from worldwatch.ingest import parsers


def test_geojson_features_filters_and_geocodes(sources):
    cfg = sources["usgs_seismic"]
    payload = load_fixture("usgs_sample.json")
    obs = parsers.parse(payload, cfg)

    # filter_min_mag = 1.0 drops the 0.7 event → 2 remain
    assert len(obs) == 2
    mags = sorted(o.value for o in obs)
    assert mags == [5.4, 6.9]

    o = next(o for o in obs if o.value == 5.4)
    assert o.stream_id == "usgs_seismic"
    assert o.ts == 1751000000  # epoch ms → s
    assert len(o.cell) > 0  # H3 cell id assigned
    assert o.meta == {"depth_km": 10.2}


def test_wikimedia_pageviews_parses_timestamps(sources):
    cfg = sources["wikipedia_pageviews"]
    payload = load_fixture("wikimedia_sample.json")
    obs = parsers.parse(payload, cfg)

    assert len(obs) == 3
    assert obs[0].value == 8123456.0
    # 2026070910 UTC → epoch
    assert obs[0].ts == 1783591200
    # non-spatial: cell is the project name
    assert obs[0].cell == "en.wikipedia"


def test_coinbase_spot_uses_now_sentinel(sources):
    cfg = sources["btc_usd"]
    payload = {"data": {"amount": "63000.42", "currency": "USD"}}
    obs = parsers.parse(payload, cfg)
    assert len(obs) == 1
    assert obs[0].value == 63000.42
    assert obs[0].cell == "GLOBAL"
    assert obs[0].ts == parsers._NOW_SENTINEL


def test_unknown_format_raises(sources):
    cfg = sources["usgs_seismic"]
    bad = type(cfg)(**{**cfg.__dict__, "parse": {"format": "nope"}})
    import pytest

    with pytest.raises(ValueError, match="No parser registered"):
        parsers.parse({}, bad)
