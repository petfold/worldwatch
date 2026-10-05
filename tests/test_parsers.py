"""Parser stability against checked-in golden fixtures."""

from conftest import FIXTURES, load_fixture
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
    import math

    cfg = sources["wikipedia_pageviews"]
    payload = load_fixture("wikimedia_sample.json")
    obs = parsers.parse(payload, cfg)

    assert len(obs) == 3
    # stanza sets transform = "log1p": views feed the continuous model on a
    # workable scale (raw views with a fixed obs_scale would pin the PIT)
    assert obs[0].value == math.log1p(8123456.0)
    # 2026070910 UTC → epoch
    assert obs[0].ts == 1783591200
    # non-spatial: cell is the project name
    assert obs[0].cell == "en.wikipedia.org"


def test_wikimedia_pageviews_raw_without_transform(sources):
    import dataclasses

    base = sources["wikipedia_pageviews"]
    cfg = dataclasses.replace(base, parse={k: v for k, v in base.parse.items() if k != "transform"})
    obs = parsers.parse(load_fixture("wikimedia_sample.json"), cfg)
    assert obs[0].value == 8123456.0


def test_coinbase_spot_uses_now_sentinel(rest_spot):
    import math

    cfg = rest_spot
    payload = {"data": {"amount": "63000.42", "currency": "USD"}}
    obs = parsers.parse(payload, cfg)
    assert len(obs) == 1
    assert obs[0].value == math.log1p(63000.42)  # stanza sets transform = "log1p"
    assert obs[0].cell == "GLOBAL"
    assert obs[0].ts == parsers._NOW_SENTINEL


def test_coinbase_spot_raw_without_transform(rest_spot):
    import dataclasses

    base = rest_spot
    cfg = dataclasses.replace(base, parse={k: v for k, v in base.parse.items() if k != "transform"})
    obs = parsers.parse({"data": {"amount": "63000.42"}}, cfg)
    assert obs[0].value == 63000.42


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

    res = sources["nws_severe_alerts"].geocode["h3_resolution"]
    assert poly.cell == h3_cell(48.21, -100.385, res)
    # onset ISO-8601 with -05:00 offset → epoch (17:30 −05:00 = 22:30 UTC),
    # plus the stable per-alert offset (id_field) that keeps same-onset alerts distinct
    assert 1783636200 <= poly.ts < 1783636200 + 600


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


def _gdelt_row(event_id: str, lat: str, lon: str, url: str | None = None) -> list[str]:
    row = ["0"] * 61
    row[0], row[56], row[57], row[59] = event_id, lat, lon, "20260711210000"
    row[60] = url or f"https://example.org/story-{event_id}"
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


def test_gdelt_counts_articles_not_coded_events(sources):
    """GDELT codes one article as several events; the unit is the article."""
    cfg = sources["gdelt_events"]
    same = "https://example.org/one-concert-many-codes"
    rows = [_gdelt_row(str(eid), "40.0", "-111.7", url=same) for eid in (1, 2, 3)]
    rows[1][31] = "9"  # the second coding is the most-mentioned
    rows.append(_gdelt_row("4", "40.0", "-111.7", url="https://example.org/another-story"))
    obs = parsers.parse(_gdelt_payload(_gdelt_zip(rows)), cfg)
    assert len(obs) == 2  # two articles, four coded events
    assert {o.context["url"] for o in obs} == {same, "https://example.org/another-story"}
    kept = next(o for o in obs if o.context["url"] == same)
    assert kept.context["mentions"] == 9


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


def _vnp46a1_payload(rad, cloud, lat, lon, dnb_qf=None, solar_zenith=None,
                     hours=1.5, time_start="2026-10-03T00:00:00.000Z"):
    """Synthetic VNP46A1 granule bytes: the datasets vnp46a1_grid reads."""
    import io

    import h5py
    import numpy as np

    rad = np.asarray(rad, "float32")
    shape = rad.shape
    buf = io.BytesIO()
    with h5py.File(buf, "w") as f:
        grid = f.create_group("HDFEOS/GRIDS/VIIRS_Grid_DNB_2d/Data Fields")
        ds = grid.create_dataset("DNB_At_Sensor_Radiance", data=rad)
        ds.attrs["_FillValue"] = np.array([-999.9], dtype="float32")
        ds = grid.create_dataset("UTC_Time", data=np.full(shape, hours, "float32"))
        ds.attrs["_FillValue"] = np.array([-999.9], dtype="float32")
        sz = np.full(shape, 120.0) if solar_zenith is None else np.asarray(solar_zenith)
        ds = grid.create_dataset("Solar_Zenith", data=np.round(sz * 100).astype("int16"))
        ds.attrs["_FillValue"] = np.array([-32768], dtype="int16")
        ds.attrs["scale_factor"] = np.array([0.01], dtype="float32")
        ds.attrs["add_offset"] = np.array([0.0], dtype="float32")
        grid.create_dataset("QF_Cloud_Mask", data=np.asarray(cloud, "uint16"))
        qf = np.zeros(shape) if dnb_qf is None else np.asarray(dnb_qf)
        grid.create_dataset("QF_DNB", data=qf.astype("uint16"))
        grid.create_dataset("lat", data=np.asarray(lat, "float64"))
        grid.create_dataset("lon", data=np.asarray(lon, "float64"))
    return {"granule_id": "TEST", "time_start": time_start, "content": buf.getvalue()}


def _nrt_cfg(sources, **parse):
    import dataclasses

    base = sources["night_lights_nrt_h18v04"]
    return dataclasses.replace(
        base,
        geocode={**base.geocode, "h3_resolution": 1},  # coarse: one cell
        parse={**base.parse, "block_pixels": 16, **parse},
    )


_NRT_LAT = [48.9 - 0.004 * i for i in range(32)]
_NRT_LON = [2.3 + 0.004 * j for j in range(32)]


def test_vnp46a1_real_granule_crop(sources):
    """A 192² crop of a real VNP46A1_NRT granule (Paris, 3 Oct 2026, moon 55%)."""
    import math

    from worldwatch.ingest.geocode import h3_cell

    cfg = sources["night_lights_nrt_h18v04"]
    content = (FIXTURES / "vnp46a1_nrt_paris_crop.h5").read_bytes()
    payload = {"granule_id": "LANCEMODIS:3057015263",
               "time_start": "2026-10-03T00:00:00.000Z", "content": content}
    obs = parsers.parse(payload, cfg)

    assert len(obs) >= 3
    assert len({o.cell for o in obs}) == len(obs)
    # the overpass was at ~01:38 UTC on the granule's day
    assert all(1790985600 + 5400 <= o.ts <= 1790985600 + 6600 for o in obs)
    by_cell = {o.cell: math.expm1(o.value) for o in obs}
    paris = by_cell[h3_cell(48.857, 2.352, 4)]
    assert paris > 10.0  # central Paris: tens of nW/cm²/sr above its background
    assert paris == max(by_cell.values())
    assert all(v >= 0.0 for v in by_cell.values())


def test_vnp46a1_screens_day_cloud_quality_and_snow(sources):
    import math

    import numpy as np

    cfg = _nrt_cfg(sources, background_percentile=0)
    rad = np.full((32, 32), 4.0)
    cloud = np.zeros((32, 32), dtype="uint16")
    poison = 9999.0
    rad[0:4, :] = poison
    cloud[0:4, :] = 3 << 6  # confident cloudy
    rad[4:8, :] = poison
    cloud[4:8, :] = 1  # day
    rad[8:12, :] = poison
    cloud[8:12, :] = 1 << 10  # snow/ice
    rad[12:16, :] = poison
    cloud[12:16, :] = 1 << 9  # cirrus
    rad[16:20, :] = poison
    qf = np.zeros((32, 32))
    qf[16:20, :] = 4  # saturation
    rad[20:22, :] = poison
    sz = np.full((32, 32), 120.0)
    sz[20:22, :] = 105.0  # twilight, not astronomically dark
    cloud[22:32, :] = 1 << 6  # probably clear: kept

    obs = parsers.parse(
        _vnp46a1_payload(rad, cloud, _NRT_LAT, _NRT_LON, dnb_qf=qf, solar_zenith=sz),
        cfg,
    )
    assert len(obs) == 1
    assert math.isclose(obs[0].value, math.log1p(4.0), rel_tol=1e-6)
    assert obs[0].ts == 1790985600 + 5400  # 2026-10-03 + 1.5 h view time
    assert obs[0].stream_id == "night_lights_nrt_h18v04"


def test_vnp46a1_background_cancels_moonlight(sources):
    """An even glow (moonlight) over the cell leaves the value unchanged."""
    import math

    import numpy as np

    cfg = _nrt_cfg(sources)  # background_percentile defaults to 10
    rad = np.full((32, 32), 0.5)
    rad[:8, :8] = 40.5  # a town in a dark countryside
    cloud = np.zeros((32, 32), dtype="uint16")

    dark = parsers.parse(_vnp46a1_payload(rad, cloud, _NRT_LAT, _NRT_LON), cfg)
    moonlit = parsers.parse(_vnp46a1_payload(rad + 2.0, cloud, _NRT_LAT, _NRT_LON), cfg)
    assert len(dark) == len(moonlit) == 1
    assert math.isclose(dark[0].value, math.log1p(40.0 * 64 / 1024), rel_tol=1e-6)
    assert math.isclose(dark[0].value, moonlit[0].value, rel_tol=1e-6)

    blackout = parsers.parse(_vnp46a1_payload(np.full((32, 32), 2.5), cloud,
                                              _NRT_LAT, _NRT_LON), cfg)
    assert blackout[0].value == 0.0


def test_vnp46a1_drops_low_coverage_cells(sources):
    import numpy as np

    cfg = _nrt_cfg(sources)
    rad = np.full((32, 32), 5.0)
    cloud = np.full((32, 32), 3 << 6, dtype="uint16")  # all cloudy...
    cloud[0, 0] = 0  # ...but one pixel: coverage 1/1024 < min_valid_frac
    assert parsers.parse(_vnp46a1_payload(rad, cloud, _NRT_LAT, _NRT_LON), cfg) == []


def test_colocated_detectors_are_one_series(sources):
    """Two detectors at one site must not be mixed into one model."""
    cfg = sources["eurdep_gamma"]
    feat = lambda sid, v, lon=16.39: {  # noqa: E731
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, 48.73]},
        "properties": {"id": sid, "site_status": 1, "end_measure": "2026-09-26T06:00:00Z",
                       "value": v, "unit": "µSv/h", "name": sid},
    }
    payload = {"features": [feat("AT0002", 0.2), feat("AT0001", 0.1), feat("AT0009", 0.3, lon=16.5)]}
    obs = parsers.parse(payload, cfg)
    assert sorted((o.meta or {})["site"] for o in obs) == ["AT0001", "AT0009"]


def _s5p_item(start, mean, total=400, nodata=100, error=None):
    item = {"interval": {"from": start, "to": "x"},
            "outputs": {"gas": {"bands": {"B0": {"stats": {
                "mean": mean, "sampleCount": total, "noDataCount": nodata}}}}}}
    if error:
        item["error"] = {"type": "EXECUTION_ERROR", "message": error}
    return item


def test_cdse_s5p_stats_screens_coverage_and_errors(sources):
    import math

    from worldwatch.ingest.geocode import h3_cell

    cfg = sources["s5p_no2"]  # scale 1e6, log1p
    payload = [
        {"box": "paris", "status": 200, "data": [
            _s5p_item("2026-10-02T18:51:00Z", 1.0e-4),                 # kept
            _s5p_item("2026-10-03T18:51:00Z", 2.0e-4, nodata=350),     # 12.5% valid: dropped
            _s5p_item("2026-10-04T18:51:00Z", None, nodata=400),       # all cloud: dropped
            _s5p_item("2026-10-05T18:51:00Z", 1.0e-4, error="boom"),   # failed interval: dropped
            _s5p_item("2026-10-06T18:51:00Z", -3.0e-6),                # noise below zero → 0
        ]},
        {"box": "tokyo", "status": 503, "data": []},
        {"box": "atlantis", "status": 200, "data": [_s5p_item("2026-10-02T18:51:00Z", 1.0)]},
    ]
    obs = parsers.parse(payload, cfg)
    assert [round(o.value, 6) for o in obs] == [round(math.log1p(100.0), 6), 0.0]
    assert obs[0].cell == h3_cell(48.86, 2.35, 3)
    # 2 Oct window (18:51 UTC) → Paris's 3 Oct overpass at 13:20:36 UTC
    assert obs[0].ts == 1790985600 + 48036


def test_cdse_s5p_stats_so2_stays_linear(sources):
    cfg = sources["s5p_so2"]  # scale 1e3, no transform: noise may go negative
    payload = [{"box": "etna", "status": 200, "data": [
        _s5p_item("2026-10-02T18:00:00Z", -2.0e-4), _s5p_item("2026-10-03T18:00:00Z", 5.0e-3)]}]
    assert [round(o.value, 6) for o in parsers.parse(payload, cfg)] == [-0.2, 5.0]


def test_goes_fdc_real_scan_crop(sources):
    """A crop of a real GOES-19 full-disk FDC scan (5 Oct 2026 05:30 UTC,
    eastern Amazon, Pará): 17 fire pixels in 5 cells. Geolocation was
    checked on the full scan: a median 1 km from a VIIRS hotspot."""
    import h3

    cfg = sources["goes19_fire"]
    obs = parsers.parse({"key": "k", "content": (FIXTURES / "goes19_fdcf_crop.nc").read_bytes()}, cfg)

    assert len(obs) == 17
    assert len({o.cell for o in obs}) == 5
    assert len({(o.cell, o.ts) for o in obs}) == 17  # same-cell fires spread by a second each
    start = 1791178221  # time_coverage_start 2026-10-05T05:30:21Z
    assert all(start <= o.ts < start + 570 for o in obs)
    assert round(sum(o.value for o in obs), 1) == 1814.3  # FRP, MW
    for o in obs:  # all in Pará, Brazil
        lat, lon = h3.cell_to_latlng(o.cell)
        assert -5 < lat < -2 and -56 < lon < -49


def test_goes_fdc_fire_codes_select_pixels(sources):
    """The crop's fires are all code 30 (temporally filtered good fire)."""
    import dataclasses

    cfg = sources["goes19_fire"]
    payload = {"key": "k", "content": (FIXTURES / "goes19_fdcf_crop.nc").read_bytes()}
    low_only = dataclasses.replace(cfg, parse={**cfg.parse, "fire_codes": [15, 35]})
    assert parsers.parse(payload, low_only) == []


def test_lsasaf_frp_list_real_slot(sources):
    """A real Meteosat 0° FRP-PIXEL ListProduct (5 Oct 2026 00:00 UTC; contains
    data from EUMETSAT LSA SAF, CC BY 4.0): 125 fires, 104 at confidence ≥ 0.5."""
    cfg = sources["meteosat_fire"]
    content = (FIXTURES / "lsasaf_msg_frp_list_202610050000.h5").read_bytes()
    obs = parsers.parse({"key": "k", "content": content}, cfg)

    assert len(obs) == 104
    assert len({(o.cell, o.ts) for o in obs}) == 104
    slot = 1791158400  # 2026-10-05 00:00 UTC
    assert all(slot + 180 <= o.ts < slot + 900 for o in obs)  # ACQTIME 3–11 min into the slot
    assert round(sum(o.value for o in obs), 1) == 11214.9  # FRP, MW


def test_lsasaf_frp_list_confidence_threshold(sources):
    import dataclasses

    cfg = sources["meteosat_fire"]
    payload = {"key": "k", "content": (FIXTURES / "lsasaf_msg_frp_list_202610050000.h5").read_bytes()}
    everything = dataclasses.replace(cfg, parse={**cfg.parse, "min_confidence": 0.0})
    strict = dataclasses.replace(cfg, parse={**cfg.parse, "min_confidence": 0.7})
    assert len(parsers.parse(payload, everything)) == 125
    assert len(parsers.parse(payload, strict)) == 82
