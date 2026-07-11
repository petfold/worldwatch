"""Fetchers: bearer auth on json_get, the Earthdata granule flow, token reuse."""

import json

import httpx
import pytest

from conftest import load_fixture
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


def test_unknown_fetch_kind_raises(sources):
    import dataclasses

    cfg = dataclasses.replace(sources["usgs_seismic"], fetch={"kind": "carrier_pigeon"})
    with pytest.raises(ValueError, match="No fetcher registered"):
        fetch.get_fetcher(cfg)


def test_stanzas_parse_fetch_table(sources):
    assert sources["night_lights_h17v03"].fetch["kind"] == "earthdata_granule"
    assert sources["usgs_seismic"].fetch == {}
    assert json.dumps(sources["cf_radar_netflows_global"].parse)  # sanity: serializable
