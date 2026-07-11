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
    assert obs[0].cell == "en.wikipedia.org"


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


def test_geojson_events_alerts(sources):
    cfg = sources["nws_severe_alerts"]
    payload = load_fixture("nws_alerts_sample.json")
    obs = parsers.parse(payload, cfg)

    # null-geometry (zone-only) alert is skipped; polygon + point remain
    assert len(obs) == 2
    for o in obs:
        assert o.stream_id == "nws_severe_alerts"
        assert o.value is None  # pure-event → count flavor
        assert len(o.cell) > 0

    # polygon reduced to its ring centroid, near (-100.385, 48.21)
    poly = obs[0]
    from worldwatch.ingest.geocode import h3_cell

    assert poly.cell == h3_cell(48.21, -100.385, 3)
    # onset ISO-8601 with -05:00 offset → epoch (17:30 −05:00 = 22:30 UTC)
    assert poly.ts == 1783636200


def test_geojson_events_time_fallback(sources):
    """onset=null falls back to effective."""
    cfg = sources["nws_severe_alerts"]
    payload = load_fixture("nws_alerts_sample.json")
    point = parsers.parse(payload, cfg)[1]  # the Point/Tornado alert, onset null
    # effective 18:05 −05:00 = 23:05 UTC
    assert point.ts == 1783638300


def test_parse_event_time_handles_ms_and_seconds():
    assert parsers._parse_event_time(1751000000000) == 1751000000  # ms
    assert parsers._parse_event_time(1751000000) == 1751000000  # seconds
    assert parsers._parse_event_time("2026-07-09T22:30:00Z") == 1783636200
    assert parsers._parse_event_time("not-a-time") is None


def test_cloudflare_radar_drops_in_progress_bucket(sources):
    cfg = sources["cf_radar_netflows_global"]
    payload = load_fixture("cloudflare_radar_sample.json")
    obs = parsers.parse(payload, cfg)

    # 4 buckets in the fixture; the final (in-progress) one is dropped
    assert len(obs) == 3
    assert [o.value for o in obs] == [0.922149, 0.930001, 0.9115]
    assert obs[0].ts == 1783710000  # 2026-07-10T19:00:00Z
    assert obs[1].ts - obs[0].ts == 900
    assert all(o.cell == "GLOBAL" for o in obs)


def test_cloudflare_radar_country_gets_centroid_h3_cell(sources):
    from worldwatch.ingest.geocode import h3_cell

    cfg = sources["cf_radar_netflows_gb"]
    payload = load_fixture("cloudflare_radar_sample.json")
    obs = parsers.parse(payload, cfg)
    assert obs[0].cell == h3_cell(54.0, -2.5, 2)


GDELT_BATCH_EPOCH = 1783803600  # 20260711210000 UTC, the fixture's batch stamp


def _gdelt_payload(content: bytes) -> dict:
    return {"batch_url": "x", "batch_epoch": GDELT_BATCH_EPOCH, "content": content}


def test_gdelt_export_geocodes_and_drops_ungeocoded(sources):
    import pathlib

    from worldwatch.ingest.geocode import h3_cell

    cfg = sources["gdelt_events"]
    content = (pathlib.Path(__file__).parent / "fixtures" / "gdelt_export_sample.zip").read_bytes()
    obs = parsers.parse(_gdelt_payload(content), cfg)

    # fixture: 3 geocoded rows (2× Utah, 1× Australia) + 1 ungeocoded (dropped)
    assert len(obs) == 3
    cells = {o.cell for o in obs}
    assert cells == {
        h3_cell(40.2222, -111.659, 3),
        h3_cell(40.1135, -111.854, 3),
        h3_cell(-36.7582, 144.28, 3),
    }
    for o in obs:
        assert o.value is None  # pure-event → count flavor
        assert GDELT_BATCH_EPOCH - 900 <= o.ts < GDELT_BATCH_EPOCH


def _gdelt_zip(rows: list[list[str]]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.export.CSV", "\n".join("\t".join(r) for r in rows) + "\n")
    return buf.getvalue()


def _gdelt_row(event_id: str, lat: str, lon: str) -> list[str]:
    row = ["0"] * 61
    row[0], row[56], row[57], row[59] = event_id, lat, lon, "20260711210000"
    return row


def test_gdelt_same_cell_events_get_distinct_deterministic_ts(sources):
    cfg = sources["gdelt_events"]
    rows = [_gdelt_row(str(eid), "40.0", "-111.7") for eid in (30, 10, 20)]
    obs = parsers.parse(_gdelt_payload(_gdelt_zip(rows)), cfg)

    # one cell, three events → three rows with distinct spread timestamps
    assert len(obs) == 3
    assert len({o.cell for o in obs}) == 1
    ts = [o.ts for o in obs]
    assert len(set(ts)) == 3  # collision-free despite identical batch stamp
    assert ts == sorted(ts)
    assert ts[0] == GDELT_BATCH_EPOCH - 900  # spread ordered by event id

    # deterministic: re-parsing the same batch yields identical rows (PK dedup)
    assert parsers.parse(_gdelt_payload(_gdelt_zip(rows)), cfg) == obs


def test_gdelt_tolerates_malformed_lines(sources):
    cfg = sources["gdelt_events"]
    rows = [
        _gdelt_row("1", "40.0", "-111.7"),
        ["7", "short", "row"],  # too few columns
        _gdelt_row("2", "not-a-lat", "-111.7"),  # unparseable coords
        _gdelt_row("3", "91.0", "-111.7"),  # out-of-range latitude
    ]
    obs = parsers.parse(_gdelt_payload(_gdelt_zip(rows)), cfg)
    assert len(obs) == 1


def _vnp46a2_payload(ntl, quality, lat, lon, time_start="2026-07-02T00:00:00.000Z"):
    """Synthetic VNP46A2 granule bytes mirroring the real group layout."""
    import io

    import h5py
    import numpy as np

    buf = io.BytesIO()
    with h5py.File(buf, "w") as f:
        grid = f.create_group("HDFEOS/GRIDS/VIIRS_Grid_DNB_2d/Data Fields")
        ds = grid.create_dataset("DNB_BRDF-Corrected_NTL", data=np.asarray(ntl, "float32"))
        ds.attrs["_FillValue"] = np.array([-999.9], dtype="float32")
        grid.create_dataset("Mandatory_Quality_Flag", data=np.asarray(quality, "uint8"))
        grid.create_dataset("lat", data=np.asarray(lat, "float64"))
        grid.create_dataset("lon", data=np.asarray(lon, "float64"))
    return {"granule_id": "TEST", "time_start": time_start, "content": buf.getvalue()}


def _nl_cfg(sources, **geocode):
    import dataclasses

    base = sources["night_lights_h17v03"]
    return dataclasses.replace(
        base, geocode={**base.geocode, **geocode}, parse={**base.parse, "block_pixels": 16}
    )


def test_vnp46a2_masks_fill_and_poor_quality(sources):
    import math

    import numpy as np

    cfg = _nl_cfg(sources, h3_resolution=1)  # coarse: all blocks land in one cell
    ntl = np.full((32, 32), 3.0)
    quality = np.zeros((32, 32), dtype="uint8")
    # poison half the pixels: fill value or poor quality — all must be excluded
    ntl[:, 16:] = -999.9
    quality[:16, :16] = 0
    quality[16:, :16] = 1
    ntl[16:, :16] = 9999.0  # poor-quality pixels carry garbage values
    lat = np.linspace(50.0, 49.9, 32)
    lon = np.linspace(-3.0, -2.9, 32)

    obs = parsers.parse(_vnp46a2_payload(ntl, quality, lat, lon), cfg)
    assert len(obs) == 1
    # only the qf==0, non-fill quadrant (value 3.0) survives the mask
    assert math.isclose(obs[0].value, math.log1p(3.0))
    assert obs[0].ts == 1782950400  # 2026-07-02T00:00:00Z
    assert obs[0].stream_id == "night_lights_h17v03"


def test_vnp46a2_drops_low_coverage_cells(sources):
    import numpy as np

    cfg = _nl_cfg(sources, h3_resolution=1)
    ntl = np.full((32, 32), 5.0)
    quality = np.full((32, 32), 255, dtype="uint8")  # no retrieval anywhere...
    quality[0, 0] = 0  # ...except one pixel: coverage 1/1024 < min_valid_frac
    lat = np.linspace(50.0, 49.9, 32)
    lon = np.linspace(-3.0, -2.9, 32)

    obs = parsers.parse(_vnp46a2_payload(ntl, quality, lat, lon), cfg)
    assert obs == []


def test_vnp46a2_groups_blocks_into_h3_cells(sources):
    import math

    import numpy as np

    cfg = _nl_cfg(sources, h3_resolution=1)
    # top 16 rows near lat 50 (value 1.0), bottom 16 near lat 20 (value 4.0):
    # far apart → two res-1 cells with distinct means
    ntl = np.vstack([np.full((16, 32), 1.0), np.full((16, 32), 4.0)])
    quality = np.zeros((32, 32), dtype="uint8")
    lat = np.concatenate([np.linspace(50.0, 49.9, 16), np.linspace(20.0, 19.9, 16)])
    lon = np.linspace(-3.0, -2.9, 32)

    obs = parsers.parse(_vnp46a2_payload(ntl, quality, lat, lon), cfg)
    assert len(obs) == 2
    values = sorted(o.value for o in obs)
    assert math.isclose(values[0], math.log1p(1.0))
    assert math.isclose(values[1], math.log1p(4.0))
    assert len({o.cell for o in obs}) == 2
