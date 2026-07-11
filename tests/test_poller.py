"""Poller: conditional GET, failure isolation, health instrumentation."""

import httpx
import pytest

from conftest import load_fixture
from worldwatch.poll.http import CacheValidators
from worldwatch.poll.poller import jitter_seconds, poll_once


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_poll_once_ok_writes_rows_and_health(db, sources):
    cfg = sources["usgs_seismic"]
    payload = load_fixture("usgs_sample.json")

    def handler(request):
        assert request.headers["User-Agent"].startswith("worldwatch/")
        return httpx.Response(200, json=payload, headers={"ETag": "v1"})

    async with _client(handler) as client:
        outcome = await poll_once(client, db, cfg, CacheValidators(), now=1751000200)

    assert outcome.event == "ok"
    assert outcome.rows_written == 2  # filter drops the 0.7 mag event
    rows = db.execute("SELECT COUNT(*) FROM raw_ring").fetchone()[0]
    assert rows == 2
    health = db.execute("SELECT event, detail FROM health WHERE event='ok'").fetchone()
    assert health["detail"] == "rows=2"


async def test_poll_once_sends_conditional_headers_after_etag(db, sources):
    cfg = sources["usgs_seismic"]
    payload = load_fixture("usgs_sample.json")
    seen_headers = []

    def handler(request):
        seen_headers.append(dict(request.headers))
        return httpx.Response(200, json=payload, headers={"ETag": "abc123"})

    v = CacheValidators()
    async with _client(handler) as client:
        await poll_once(client, db, cfg, v, now=1751000200)
        assert v.etag == "abc123"
        await poll_once(client, db, cfg, v, now=1751000500)

    assert "if-none-match" not in seen_headers[0]
    assert seen_headers[1]["if-none-match"] == "abc123"


async def test_poll_once_304_records_not_modified(db, sources):
    cfg = sources["usgs_seismic"]

    def handler(request):
        return httpx.Response(304)

    async with _client(handler) as client:
        outcome = await poll_once(client, db, cfg, CacheValidators(etag="x"), now=1751000200)

    assert outcome.event == "not_modified"
    assert db.execute("SELECT COUNT(*) FROM raw_ring").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM health WHERE event='not_modified'").fetchone()[0] == 1


async def test_poll_once_http_error_isolated(db, sources):
    cfg = sources["usgs_seismic"]

    def handler(request):
        return httpx.Response(503)

    async with _client(handler) as client:
        outcome = await poll_once(client, db, cfg, CacheValidators(), now=1751000200)

    # never raises; classified and instrumented
    assert outcome.event == "http_error"
    assert db.execute("SELECT COUNT(*) FROM health WHERE event='http_error'").fetchone()[0] == 1


async def test_poll_once_parse_error_isolated(db, sources):
    cfg = sources["usgs_seismic"]

    def handler(request):
        return httpx.Response(200, json={"unexpected": "shape"})

    async with _client(handler) as client:
        outcome = await poll_once(client, db, cfg, CacheValidators(), now=1751000200)

    # geojson parser tolerates missing "features" → 0 rows, still "ok".
    # Force a real parse error with a non-dict payload instead:
    def bad_handler(request):
        return httpx.Response(200, json=[1, 2, 3])

    async with _client(bad_handler) as client:
        outcome = await poll_once(client, db, cfg, CacheValidators(), now=1751000200)
    assert outcome.event == "parse_error"


async def test_poll_once_timeout_isolated(db, sources):
    cfg = sources["usgs_seismic"]

    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    async with _client(handler) as client:
        outcome = await poll_once(client, db, cfg, CacheValidators(), now=1751000200)

    assert outcome.event == "timeout"
    assert db.execute("SELECT COUNT(*) FROM health WHERE event='timeout'").fetchone()[0] == 1


async def test_templated_endpoint_is_fetched(db, sources):
    """The poller fetches the date-filled URL, not the raw template."""
    cfg = sources["wikipedia_pageviews"]
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, json={"items": []})

    async with _client(handler) as client:
        await poll_once(client, db, cfg, CacheValidators(), now=1_783_641_600)

    assert "{" not in requested[0]
    assert "/hourly/" in requested[0] and requested[0].endswith("2026071000")


async def test_spot_price_stamped_with_poll_time(db, sources):
    cfg = sources["btc_usd"]

    def handler(request):
        return httpx.Response(200, json={"data": {"amount": "63000.42"}})

    async with _client(handler) as client:
        await poll_once(client, db, cfg, CacheValidators(), now=1751000200)

    import math

    row = db.execute("SELECT ts, value FROM raw_ring").fetchone()
    assert row["ts"] == 1751000200  # sentinel replaced by poll time
    assert row["value"] == math.log1p(63000.42)  # stanza sets transform = "log1p"


def test_jitter_is_deterministic_and_bounded(sources):
    cfg = sources["usgs_seismic"]
    j1 = jitter_seconds(cfg.stream_id, cfg.cadence_seconds)
    j2 = jitter_seconds(cfg.stream_id, cfg.cadence_seconds)
    assert j1 == j2  # deterministic
    assert 0 <= j1 < 0.25 * cfg.cadence_seconds


@pytest.mark.parametrize(
    "sid,payload",
    [
        ("usgs_seismic", load_fixture("usgs_sample.json")),
        ("wikipedia_pageviews", load_fixture("wikimedia_sample.json")),
        ("btc_usd", {"data": {"amount": "100.0"}}),
        (
            "safecast_radiation",
            [
                {
                    "value": 30.0,
                    "latitude": 35.0,
                    "longitude": 139.0,
                    "captured_at": "2026-07-09T10:00:00Z",
                }
            ],
        ),
    ],
)
async def test_idempotent_reingest(db, sources, sid, payload):
    """Re-polling the same payload writes no duplicate rows (crash-restart safe)."""
    cfg = sources[sid]

    def handler(request):
        return httpx.Response(200, json=payload)

    async with _client(handler) as client:
        await poll_once(client, db, cfg, CacheValidators(), now=1751000200)
        first = db.execute("SELECT COUNT(*) FROM raw_ring").fetchone()[0]
        await poll_once(client, db, cfg, CacheValidators(), now=1751000200)
        second = db.execute("SELECT COUNT(*) FROM raw_ring").fetchone()[0]

    assert first == second
    assert first > 0
