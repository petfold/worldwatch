"""Fetchers: bearer auth on json_get, the Earthdata granule flow, token reuse."""

import json
import math

import httpx
import pytest

from conftest import load_fixture
from worldwatch.ingest.geocode import h3_cell
from worldwatch.poll import fetch
from worldwatch.poll.http import CacheValidators
from worldwatch.poll.poller import poll_once


@pytest.fixture(autouse=True)
def _clean_token_cache():
    fetch._edl_tokens.clear()
    yield
    fetch._edl_tokens.clear()


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- json_get + Bearer auth --------------------------------------------------


async def test_json_get_sends_bearer_token(db, sources, monkeypatch):
    monkeypatch.setenv("WW_CLOUDFLARE_TOKEN", "cf-secret")
    cfg = sources["cf_radar_netflows_global"]
    payload = load_fixture("cloudflare_radar_sample.json")
    seen = []

    def handler(request):
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200, json=payload)

    async with _client(handler) as client:
        outcome = await poll_once(client, db, cfg, CacheValidators(), now=1783970100)

    assert outcome.event == "ok"
    assert outcome.rows_written == 3
    assert seen == ["Bearer cf-secret"]


async def test_json_get_auth_scheme_keeps_the_key_out_of_the_url(db, sources, monkeypatch):
    monkeypatch.setenv("WW_OXR_APP_ID", "0123456789abcdef0123456789abcdef")
    cfg = sources["fx_usd"]
    payload = load_fixture("oxr_latest.json")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=payload)

    async with _client(handler) as client:
        outcome = await poll_once(client, db, cfg, CacheValidators(), now=1790741000)

    assert outcome.event == "ok"
    assert outcome.rows_written == 9  # the watched currencies; USD, DKK, BTC, gold dropped
    assert seen[0].headers["Authorization"] == "Token 0123456789abcdef0123456789abcdef"
    assert "0123456789abcdef" not in str(seen[0].url)


async def test_a_url_key_is_sent_only_in_the_url(db, sources, monkeypatch):
    """EIA takes its key only in the URL: no Authorization header besides."""
    monkeypatch.setenv("WW_EIA_KEY", "eia-secret-123")
    cfg = sources["eia_demand"]
    payload = load_fixture("eia_region.json")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=payload)

    async with _client(handler) as client:
        outcome = await poll_once(client, db, cfg, CacheValidators(), now=1790741000)

    assert outcome.event == "ok" and outcome.rows_written > 0
    assert "api_key=eia-secret-123" in str(seen[0].url)
    assert "Authorization" not in seen[0].headers


async def test_a_failed_fetch_records_the_error_without_the_key(db, sources, monkeypatch):
    """An httpx error quotes the URL, key and all; the health table (and the
    export that copies it off the VPS) gets it redacted."""
    monkeypatch.setenv("WW_EIA_KEY", "eia-secret-123")
    cfg = sources["eia_demand"]

    async with _client(lambda request: httpx.Response(403, text="forbidden")) as client:
        outcome = await poll_once(client, db, cfg, CacheValidators(), now=1790741000)

    detail = db.execute("SELECT detail FROM health WHERE component = 'eia_demand'").fetchone()["detail"]
    assert outcome.event == "http_error"
    assert "eia-secret-123" not in detail and "eia-secret-123" not in (outcome.detail or "")
    assert "api_key=***" in detail


def test_fx_cadence_stays_inside_the_free_plan(sources):
    """1,000 requests a month, and a 304 counts too: the cadence is the budget."""
    assert 31 * 86400 / sources["fx_usd"].cadence_seconds < 1000


async def test_json_get_missing_auth_env_is_isolated(db, sources, monkeypatch):
    monkeypatch.delenv("WW_CLOUDFLARE_TOKEN", raising=False)
    cfg = sources["cf_radar_netflows_global"]

    def handler(request):  # pragma: no cover - must not be reached
        raise AssertionError("no request should be made without the token")

    async with _client(handler) as client:
        outcome = await poll_once(client, db, cfg, CacheValidators(), now=1783970100)

    assert outcome.event == "fetch_error"
    assert "WW_CLOUDFLARE_TOKEN" in (outcome.detail or "")
    assert db.execute("SELECT COUNT(*) FROM health WHERE event='fetch_error'").fetchone()[0] == 1


# --- earthdata_granule -------------------------------------------------------

GRANULE_ID = "VNP46A2.A2026183.h17v03.002.2026191141019"


def _cmr_entry():
    return {
        "title": GRANULE_ID,
        "time_start": "2026-07-02T00:00:00.000Z",
        "links": [
            {"href": "https://ladsweb.modaps.eosdis.nasa.gov/opendap/x.h5.html"},
            {"href": f"https://data.laadsdaac.earthdatacloud.nasa.gov/prod-lads/VNP46A2/{GRANULE_ID}.h5"},
        ],
    }


def _granule_bytes() -> bytes:
    """A tiny but real HDF5 granule the vnp46a2_grid parser can read."""
    import io

    import h5py
    import numpy as np

    buf = io.BytesIO()
    with h5py.File(buf, "w") as f:
        grid = f.create_group("HDFEOS/GRIDS/VIIRS_Grid_DNB_2d/Data Fields")
        ds = grid.create_dataset(
            "DNB_BRDF-Corrected_NTL", data=np.full((16, 16), 2.0, dtype="float32")
        )
        ds.attrs["_FillValue"] = np.array([-999.9], dtype="float32")
        grid.create_dataset("Mandatory_Quality_Flag", data=np.zeros((16, 16), dtype="uint8"))
        grid.create_dataset("lat", data=np.linspace(55.0, 54.9, 16))
        grid.create_dataset("lon", data=np.linspace(-3.0, -2.9, 16))
    return buf.getvalue()


def _earthdata_handler(counters, tokens_on_urs):
    granule = _granule_bytes()

    def handler(request):
        host, path = request.url.host, request.url.path
        if host == "cmr.earthdata.nasa.gov":
            counters["cmr"] += 1
            return httpx.Response(200, json={"feed": {"entry": [_cmr_entry()]}})
        if host == "urs.earthdata.nasa.gov" and path == "/api/users/tokens":
            counters["urs_list"] += 1
            assert request.headers["Authorization"].startswith("Basic ")
            return httpx.Response(200, json=tokens_on_urs)
        if host == "urs.earthdata.nasa.gov" and path == "/api/users/token":
            counters["urs_mint"] += 1
            return httpx.Response(200, json={"access_token": "minted-token"})
        if host == "data.laadsdaac.earthdatacloud.nasa.gov":
            counters["download"] += 1
            assert request.headers["Authorization"].startswith("Bearer ")
            return httpx.Response(200, content=granule)
        raise AssertionError(f"unexpected request: {request.url}")

    return handler


async def test_earthdata_granule_end_to_end(db, sources, monkeypatch):
    monkeypatch.setenv("WW_EARTHDATA_USER", "peter")
    monkeypatch.setenv("WW_EARTHDATA_PASS", "pw")
    monkeypatch.delenv("WW_EARTHDATA_TOKEN", raising=False)
    cfg = sources["night_lights_h17v03"]
    counters = dict.fromkeys(("cmr", "urs_list", "urs_mint", "download"), 0)
    validators = CacheValidators()

    async with _client(_earthdata_handler(counters, tokens_on_urs=[])) as client:
        outcome = await poll_once(client, db, cfg, validators, now=1783970100)

        assert outcome.event == "ok"
        assert outcome.rows_written > 0
        # no token existed on URS → exactly one minted, then cached
        assert counters["urs_mint"] == 1
        # granule day carried through: all rows at 2026-07-02T00:00:00Z
        ts = {r["ts"] for r in db.execute("SELECT ts FROM raw_ring").fetchall()}
        assert ts == {1782950400}
        # the granule id memo lives in the ETag validator slot
        assert validators.etag == GRANULE_ID

        # second poll: same granule id → skipped without a download
        outcome2 = await poll_once(client, db, cfg, validators, now=1783991700)

    assert outcome2.event == "not_modified"
    assert counters["download"] == 1
    assert counters["cmr"] == 2


async def test_earthdata_nrt_granule_from_lance(db, sources, monkeypatch):
    """VNP46A1_NRT: CMR lists the file on nrt3.modaps (LANCE), not earthdatacloud."""
    from conftest import FIXTURES

    monkeypatch.setenv("WW_EARTHDATA_TOKEN", "edl-token")
    cfg = sources["night_lights_nrt_h18v04"]
    name = "VNP46A1_NRT.A2026276.h18v04.002.2026277050716.h5"
    entry = {  # trimmed from the live CMR response, 5 Oct 2026
        "title": "LANCEMODIS:3057015263",
        "time_start": "2026-10-03T00:00:00.000Z",
        "links": [
            {"href": f"https://nrt3.modaps.eosdis.nasa.gov/api/v2/content/archives/allData/5200/VNP46A1_NRT/2026/276/{name}"},
            {"href": "http://doi.org/10.5067/VIIRS/VNP46A1_NRT.002"},
            {"href": "https://nrt3.modaps.eosdis.nasa.gov/archive/allData/5200/VNP46A1_NRT/"},
        ],
    }
    downloads = []

    def handler(request):
        if request.url.host == "cmr.earthdata.nasa.gov":
            assert request.url.params["short_name"] == "VNP46A1_NRT"
            return httpx.Response(200, json={"feed": {"entry": [entry]}})
        if request.url.host == "nrt3.modaps.eosdis.nasa.gov":
            downloads.append(request.url.path)
            assert request.headers["Authorization"] == "Bearer edl-token"
            return httpx.Response(200, content=(FIXTURES / "vnp46a1_nrt_paris_crop.h5").read_bytes())
        raise AssertionError(f"unexpected request: {request.url}")

    async with _client(handler) as client:
        outcome = await poll_once(client, db, cfg, CacheValidators(), now=1791200000)

    assert outcome.event == "ok"
    assert outcome.rows_written >= 3
    assert downloads == [f"/api/v2/content/archives/allData/5200/VNP46A1_NRT/2026/276/{name}"]


async def test_earthdata_token_reused_from_urs_and_cached(db, sources, monkeypatch):
    monkeypatch.setenv("WW_EARTHDATA_USER", "peter")
    monkeypatch.setenv("WW_EARTHDATA_PASS", "pw")
    monkeypatch.delenv("WW_EARTHDATA_TOKEN", raising=False)
    counters = dict.fromkeys(("cmr", "urs_list", "urs_mint", "download"), 0)
    existing = [{"access_token": "already-minted"}]

    async with _client(_earthdata_handler(counters, tokens_on_urs=existing)) as client:
        for sid in ("night_lights_h17v03", "night_lights_h18v04"):
            outcome = await poll_once(client, db, sources[sid], CacheValidators(), now=1783970100)
            assert outcome.event == "ok"

    assert counters["urs_mint"] == 0  # reused the token listed on URS...
    assert counters["urs_list"] == 1  # ...and hit URS only once for both tiles
    assert fetch._edl_tokens == {"peter": "already-minted"}


async def test_earthdata_missing_credentials_is_isolated(db, sources, monkeypatch):
    for var in ("WW_EARTHDATA_USER", "WW_EARTHDATA_PASS", "WW_EARTHDATA_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    cfg = sources["night_lights_h17v03"]

    def handler(request):
        if request.url.host == "cmr.earthdata.nasa.gov":
            return httpx.Response(200, json={"feed": {"entry": [_cmr_entry()]}})
        raise AssertionError("must not reach auth/download without credentials")

    async with _client(handler) as client:
        outcome = await poll_once(client, db, cfg, CacheValidators(), now=1783970100)

    assert outcome.event == "fetch_error"
    assert "WW_EARTHDATA_USER" in (outcome.detail or "")


async def test_earthdata_cmr_empty_is_isolated(db, sources, monkeypatch):
    monkeypatch.setenv("WW_EARTHDATA_USER", "peter")
    monkeypatch.setenv("WW_EARTHDATA_PASS", "pw")
    cfg = sources["night_lights_h17v03"]

    def handler(request):
        return httpx.Response(200, json={"feed": {"entry": []}})

    async with _client(handler) as client:
        outcome = await poll_once(client, db, cfg, CacheValidators(), now=1783970100)

    assert outcome.event == "fetch_error"
    assert "no granules" in (outcome.detail or "")


async def test_earthdata_expired_token_cache_cleared_on_403(db, sources, monkeypatch):
    monkeypatch.setenv("WW_EARTHDATA_USER", "peter")
    monkeypatch.setenv("WW_EARTHDATA_PASS", "pw")
    monkeypatch.delenv("WW_EARTHDATA_TOKEN", raising=False)
    fetch._edl_tokens["peter"] = "stale-token"
    cfg = sources["night_lights_h17v03"]

    def handler(request):
        if request.url.host == "cmr.earthdata.nasa.gov":
            return httpx.Response(200, json={"feed": {"entry": [_cmr_entry()]}})
        if request.url.host == "data.laadsdaac.earthdatacloud.nasa.gov":
            return httpx.Response(403)
        raise AssertionError(f"unexpected request: {request.url}")

    async with _client(handler) as client:
        outcome = await poll_once(client, db, cfg, CacheValidators(), now=1783970100)

    assert outcome.event == "http_error"
    assert fetch._edl_tokens == {}  # next poll re-mints instead of looping on 403


# --- gdelt_lastupdate --------------------------------------------------------

GDELT_URL = "http://data.gdeltproject.org/gdeltv2/20260711210000.export.CSV.zip"
GDELT_LISTING = (
    f"39218 dee38266f156e8f84e173f0ca02b9a1d {GDELT_URL}\n"
    "49321 d9d3c1d2da0893e22ad80eccfc009de1 "
    "http://data.gdeltproject.org/gdeltv2/20260711210000.mentions.CSV.zip\n"
)


def _gdelt_handler(counters):
    import pathlib

    zip_bytes = (
        pathlib.Path(__file__).parent / "fixtures" / "gdelt_export_sample.zip"
    ).read_bytes()

    def handler(request):
        if str(request.url).endswith("lastupdate.txt"):
            counters["listing"] += 1
            return httpx.Response(200, text=GDELT_LISTING)
        if str(request.url) == GDELT_URL:
            counters["download"] += 1
            return httpx.Response(200, content=zip_bytes)
        raise AssertionError(f"unexpected request: {request.url}")

    return handler


async def test_gdelt_end_to_end_and_batch_memo(db, sources):
    cfg = sources["gdelt_events"]
    counters = {"listing": 0, "download": 0}
    validators = CacheValidators()

    async with _client(_gdelt_handler(counters)) as client:
        outcome = await poll_once(client, db, cfg, validators, now=1783803700)
        assert outcome.event == "ok"
        assert outcome.rows_written == 3  # 3 geocoded fixture events
        # spread timestamps sit inside the batch's 15-min window
        ts = [r["ts"] for r in db.execute("SELECT ts FROM raw_ring ORDER BY ts").fetchall()]
        assert all(1783803600 - 900 <= t < 1783803600 for t in ts)
        assert validators.etag == GDELT_URL  # batch-url memo

        outcome2 = await poll_once(client, db, cfg, validators, now=1783804600)

    assert outcome2.event == "not_modified"
    assert counters["download"] == 1
    assert counters["listing"] == 2


async def test_gdelt_cdn_404_falls_back_to_bucket(db, sources):
    # A Google CDN edge can serve a cached empty 404 for a fresh file.
    cfg = sources["gdelt_events"]
    bucket_url = GDELT_URL.replace(
        "http://data.gdeltproject.org/", "https://storage.googleapis.com/data.gdeltproject.org/"
    )
    served = _gdelt_handler({"listing": 0, "download": 0})
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if str(request.url) == GDELT_URL:
            return httpx.Response(404)
        if str(request.url) == bucket_url:
            return served(httpx.Request("GET", GDELT_URL))
        return served(request)

    async with _client(handler) as client:
        outcome = await poll_once(client, db, cfg, CacheValidators(), now=1783803700)

    assert outcome.event == "ok"
    assert outcome.rows_written == 3
    assert seen[-2:] == [GDELT_URL, bucket_url]


async def test_gdelt_listing_without_export_entry_is_isolated(db, sources):
    cfg = sources["gdelt_events"]

    def handler(request):
        return httpx.Response(200, text="malformed listing\n")

    async with _client(handler) as client:
        outcome = await poll_once(client, db, cfg, CacheValidators(), now=1783803700)

    assert outcome.event == "fetch_error"
    assert ".export.CSV.zip" in (outcome.detail or "")


def test_unknown_fetch_kind_raises(sources):
    import dataclasses

    cfg = dataclasses.replace(sources["usgs_seismic"], fetch={"kind": "carrier_pigeon"})
    with pytest.raises(ValueError, match="No fetcher registered"):
        fetch.get_fetcher(cfg)


def test_stanzas_parse_fetch_table(sources):
    assert sources["night_lights_h17v03"].fetch["kind"] == "earthdata_granule"
    assert sources["usgs_seismic"].fetch == {}
    assert json.dumps(sources["cf_radar_netflows_global"].parse)  # sanity: serializable


# --- cdse_statistics (Sentinel-5P) --------------------------------------------


def _s5p_cfg(sources, **fetch_over):
    import dataclasses

    base = sources["s5p_no2"]
    boxes = {"paris": [48.36, 1.85, 49.36, 2.85], "tokyo": [35.18, 139.19, 36.18, 140.19]}
    return dataclasses.replace(
        base, geocode={**base.geocode, "boxes": boxes},
        fetch={**base.fetch, "pause_seconds": 0, **fetch_over},
    )


def _s5p_handler(log, valid=lambda box, day: True, fail_box=None):
    """CDSE mock: a token endpoint, and P1D statistics for each requested day."""
    from datetime import datetime

    def iso_epoch(s):
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())

    def handler(request):
        if request.url.host == "identity.dataspace.copernicus.eu":
            log["token"] += 1
            form = dict(x.split("=") for x in request.content.decode().split("&"))
            assert form["grant_type"] == "client_credentials"
            assert form["client_id"] == "cid"
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 600})
        assert request.url.host == "sh.dataspace.copernicus.eu"
        assert request.headers["Authorization"] == "Bearer tok"
        body = json.loads(request.content)
        assert body["input"]["data"][0]["dataFilter"] == {"timeliness": "NRTI"}
        assert body["input"]["data"][0]["processing"] == {"minQa": 75}
        assert '"NO2", "dataMask"' in body["aggregation"]["evalscript"]
        lon = (body["input"]["bounds"]["bbox"][0] + body["input"]["bounds"]["bbox"][2]) / 2
        box = "paris" if lon < 100 else "tokyo"
        log["requests"].append(box)
        if box == fail_box:
            return httpx.Response(503, json={"error": "busy"})
        tr = body["aggregation"]["timeRange"]
        data, t = [], iso_epoch(tr["from"])
        while t < iso_epoch(tr["to"]):
            ok = valid(box, t)
            stats = {"min": 0, "max": 1, "mean": 1.0e-4 if ok else None, "stDev": 0,
                     "sampleCount": 400, "noDataCount": 100 if ok else 400}
            data.append({"interval": {"from": fetch._utc_iso(t), "to": fetch._utc_iso(t + 86400)},
                         "outputs": {"gas": {"bands": {"B0": {"stats": stats}}}}})
            t += 86400
        return httpx.Response(200, json={"data": data, "status": "OK"})

    return handler


@pytest.fixture
def _cdse_env(monkeypatch):
    monkeypatch.setenv("WW_CDSE_CLIENT_ID", "cid")
    monkeypatch.setenv("WW_CDSE_CLIENT_SECRET", "secret")
    fetch._cdse_tokens.clear()
    yield
    fetch._cdse_tokens.clear()


def test_s5p_window_follows_local_solar_time(sources):
    cfg = sources["s5p_no2"]
    # 19:00 local the evening before: Greenwich 19:00 UTC, Tokyo (~140°E) ~09:40 UTC
    assert fetch.s5p_window_offset(cfg, 0.0) == -5 * 3600
    assert fetch.s5p_window_offset(cfg, 139.69) == -51540  # -5 h - 9 h 18.76 min, to the minute


async def test_cdse_statistics_one_request_per_box_per_day(db, sources, _cdse_env):
    cfg = _s5p_cfg(sources)
    log = {"token": 0, "requests": []}
    validators = CacheValidators()
    now = 1791072000  # 2026-10-04 00:00 UTC
    async with _client(_s5p_handler(log)) as client:
        outcome = await poll_once(client, db, cfg, validators, now=now)
        assert outcome.event == "ok"
        assert sorted(log["requests"]) == ["paris", "tokyo"]
        # two days per box, each at its overpass (~13:30 local solar)
        rows = db.execute("SELECT cell, ts, value FROM raw_ring ORDER BY ts").fetchall()
        assert len(rows) == 4
        paris = h3_cell(48.86, 2.35, 3)
        # Paris's overpass, 13:30 local solar = 13:20:36 UTC, on 2 and 3 Oct
        assert [r["ts"] for r in rows if r["cell"] == paris] == [
            1790985600 - 86400 + 48036, 1790985600 + 48036]
        assert all(math.isclose(r["value"], math.log1p(100.0)) for r in rows)

        # the next poll, same day: nothing new to ask for, no request, no token
        outcome2 = await poll_once(client, db, cfg, validators, now=now + 10800)
        assert outcome2.event == "not_modified"
        assert len(log["requests"]) == 2

        # a day later each box is asked once more, with the cached token gone stale
        await poll_once(client, db, cfg, validators, now=now + 86400)
    assert len(log["requests"]) == 4
    assert log["token"] == 2  # 600 s tokens: one per poll that made requests


async def test_cdse_statistics_retries_an_empty_day_then_gives_up(db, sources, _cdse_env):
    cfg = _s5p_cfg(sources, retry_hours=12)  # Paris's day ended 5 h before `now`
    log = {"token": 0, "requests": []}
    validators = CacheValidators()
    now = 1791072000
    async with _client(_s5p_handler(log, valid=lambda box, day: box != "paris")) as client:
        await poll_once(client, db, cfg, validators, now=now)
        await poll_once(client, db, cfg, validators, now=now + 3600)
        assert log["requests"].count("paris") == 2  # still empty: asked again
        assert log["requests"].count("tokyo") == 1
        await poll_once(client, db, cfg, validators, now=now + 7 * 3600)
        await poll_once(client, db, cfg, validators, now=now + 8 * 3600)
    assert log["requests"].count("paris") == 3  # the last try past 12 h; then given up


async def test_cdse_statistics_failing_box_is_isolated(db, sources, _cdse_env):
    cfg = _s5p_cfg(sources)
    log = {"token": 0, "requests": []}
    validators = CacheValidators()
    async with _client(_s5p_handler(log, fail_box="tokyo")) as client:
        outcome = await poll_once(client, db, cfg, validators, now=1791072000)
    assert outcome.event == "ok"
    assert outcome.rows_written == 2  # paris's two days
    assert json.loads(validators.etag).keys() == {"paris"}  # tokyo asked again next poll


async def test_cdse_statistics_missing_credentials_is_isolated(db, sources, monkeypatch):
    monkeypatch.delenv("WW_CDSE_CLIENT_ID", raising=False)
    monkeypatch.delenv("WW_CDSE_CLIENT_SECRET", raising=False)

    def handler(request):
        raise AssertionError(f"unexpected request: {request.url}")

    async with _client(handler) as client:
        outcome = await poll_once(client, db, _s5p_cfg(sources), CacheValidators(), now=1791072000)
    assert outcome.event == "fetch_error"
    assert "WW_CDSE_CLIENT_ID" in (outcome.detail or "")


# --- s3_latest (GOES on NOAA NODD) --------------------------------------------


def _s3_listing(keys):
    items = "".join(f"<Contents><Key>{k}</Key><Size>1</Size></Contents>" for k in keys)
    return f'<?xml version="1.0"?><ListBucketResult>{items}</ListBucketResult>'


async def test_s3_latest_downloads_newest_scan_once(db, sources):
    from conftest import FIXTURES

    cfg = sources["goes19_fire"]
    hour = "ABI-L2-FDCF/2026/278/05/"
    keys = [hour + f"OR_ABI-L2-FDCF-M6_G19_s2026278{m}0210_e0_c0.nc" for m in ("0510", "0530", "0520")]
    keys.append(hour + "OR_ABI-L2-FDCF-M3_G19_s20262780500210_e0_c0.nc")  # older, other scan mode
    log = []

    def handler(request):
        if request.url.path == "/":
            log.append(request.url.params["prefix"])
            return httpx.Response(200, text=_s3_listing(keys))
        log.append(request.url.path)
        return httpx.Response(200, content=(FIXTURES / "goes19_fdcf_crop.nc").read_bytes())

    validators = CacheValidators()
    async with _client(handler) as client:
        now = 1791179100  # 2026-10-05 05:45 UTC
        outcome = await poll_once(client, db, cfg, validators, now=now)
        assert outcome.event == "ok" and outcome.rows_written == 17
        assert log == [hour, "/" + keys[1]]  # the 05:30 scan
        outcome2 = await poll_once(client, db, cfg, validators, now=now + 60)
    assert outcome2.event == "not_modified"
    assert log[2:] == [hour]  # listed again, not downloaded


async def test_s3_latest_falls_back_to_previous_hour(db, sources):
    cfg = sources["goes19_fire"]
    seen = []

    def handler(request):
        if request.url.path == "/":
            prefix = request.url.params["prefix"]
            seen.append(prefix)
            keys = [prefix + "OR_X_s20262780550210_e0_c0.nc"] if prefix.endswith("/05/") else []
            return httpx.Response(200, text=_s3_listing(keys))
        return httpx.Response(200, content=b"not a netcdf")

    async with _client(handler) as client:
        outcome = await poll_once(client, db, cfg, CacheValidators(), now=1791180030)  # 06:00:30
    assert seen == ["ABI-L2-FDCF/2026/278/06/", "ABI-L2-FDCF/2026/278/05/"]
    assert outcome.event == "parse_error"  # the fake bytes: fetched, then isolated at the parser
