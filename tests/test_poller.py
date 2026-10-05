"""Poller: conditional GET, failure isolation, health instrumentation."""

import httpx
import pytest

from conftest import load_fixture
from worldwatch.poll.http import CacheValidators
from worldwatch.poll.poller import jitter_seconds, poll_once, slot_delay


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


async def test_spot_price_stamped_with_poll_time(db, rest_spot):
    cfg = rest_spot

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


def test_slot_delay_lands_on_the_phase():
    hour = 1790740800  # 2026-09-30 05:00 UTC
    assert slot_delay(hour + 37 * 60, 3600, 240) == 23 * 60 + 240  # 05:37 → 06:04
    assert slot_delay(hour + 100, 3600, 240) == 140
    assert slot_delay(hour + 240, 3600, 240) == 0


async def test_phased_poller_reads_just_after_each_publication(db, sources, monkeypatch):
    """Open Exchange Rates publishes 1-2 min past the hour: read at hh:04 every
    hour, whenever the poller started (the first deploy read at 05:37)."""
    import asyncio

    from worldwatch.poll import poller

    monkeypatch.setenv("WW_OXR_APP_ID", "x")
    cfg = sources["fx_usd"]
    payload = load_fixture("oxr_latest.json")
    clock = [1790740800 + 37 * 60]
    polls: list[float] = []
    stop = asyncio.Event()

    async def fake_sleep(seconds):
        clock[0] += seconds

    def handler(request):
        polls.append(clock[0])
        if len(polls) == 3:
            stop.set()
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(poller.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(poller.time, "time", lambda: clock[0])
    async with _client(handler) as client:
        await poller.run_poller(client, db, cfg, stop=stop)

    assert [(t - 1790740800) / 60 for t in polls] == [64, 124, 184]  # 06:04, 07:04, 08:04


@pytest.mark.parametrize(
    "sid,payload",
    [
        ("usgs_seismic", load_fixture("usgs_sample.json")),
        ("wikipedia_pageviews", load_fixture("wikimedia_sample.json")),
        ("rest_spot", {"data": {"amount": "100.0"}}),
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
async def test_idempotent_reingest(db, sources, rest_spot, sid, payload):
    """Re-polling the same payload writes no duplicate rows (crash-restart safe)."""
    cfg = rest_spot if sid == "rest_spot" else sources[sid]

    def handler(request):
        return httpx.Response(200, json=payload)

    async with _client(handler) as client:
        await poll_once(client, db, cfg, CacheValidators(), now=1751000200)
        first = db.execute("SELECT COUNT(*) FROM raw_ring").fetchone()[0]
        await poll_once(client, db, cfg, CacheValidators(), now=1751000200)
        second = db.execute("SELECT COUNT(*) FROM raw_ring").fetchone()[0]

    assert first == second
    assert first > 0


async def test_validators_survive_a_restart(db, sources):
    """The poller's memo (here an ETag) is stored, so a restarted poller sends
    If-None-Match instead of downloading the same document again."""
    import asyncio

    import httpx

    from worldwatch.poll.poller import load_validators, run_poller

    cfg = sources["usgs_seismic"]
    seen = []

    def handler(request):
        seen.append(request.headers.get("If-None-Match"))
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []},
                              headers={"ETag": '"v1"'})

    async def one_run():
        stop = asyncio.Event()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            task = asyncio.create_task(run_poller(client, db, cfg, stop=stop))
            want = len(seen) + 1
            while len(seen) < want:
                await asyncio.sleep(0.01)
            stop.set()
            task.cancel()

    import worldwatch.poll.poller as poller
    orig = poller.jitter_seconds
    poller.jitter_seconds = lambda sid, cadence: 0.0
    try:
        await one_run()          # first life: downloads, remembers the ETag
        assert load_validators(db, cfg.stream_id).etag == '"v1"'
        await one_run()          # a restart: asks conditionally
    finally:
        poller.jitter_seconds = orig
    assert seen == [None, '"v1"']
