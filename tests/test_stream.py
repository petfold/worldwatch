"""Push feeds over WebSocket (ADR 0002 §D): throttling, store + live score,
presence heartbeat, reconnect, and fault isolation — with a fake socket."""

import asyncio
import dataclasses
import json
import math

import pytest

from worldwatch.ingest import parsers
from worldwatch.ingest.models import Observation
from worldwatch.poll.stream import Throttle, run_stream


def _tick(price, t="2026-09-26T13:33:27.137843Z", product="BTC-USD"):
    return json.dumps({"type": "ticker", "product_id": product, "price": str(price), "time": t})


def _emsc(action="create", mag=4.2, t="2026-09-26T08:00:52.8Z", unid="20260926_0000078"):
    return json.dumps({"action": action, "data": {"type": "Feature", "id": unid, "properties": {
        "time": t, "lat": 40.1383, "lon": 31.7025, "depth": 5.0, "mag": mag, "magtype": "ml",
        "flynn_region": "WESTERN TURKEY", "auth": "KOERI", "evtype": "ke", "unid": unid}}})


class FakeWS:
    def __init__(self, messages, stop):
        self.messages, self.stop, self.sent = list(messages), stop, []

    async def send(self, m):
        self.sent.append(json.loads(m))

    async def recv(self):
        while not self.messages:
            if self.stop.is_set():
                raise ConnectionError("closed")
            await asyncio.sleep(0.005)
        return self.messages.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _connector(sockets):
    """connect() returning the queued sockets in order (an Exception = a failed connect)."""
    queue = list(sockets)

    def connect(url, **kw):
        s = queue.pop(0)
        if isinstance(s, Exception):
            raise s
        return s

    return connect


def _fast(cfg, **fetch):
    return dataclasses.replace(cfg, fetch={**cfg.fetch, "flush_seconds": 0.01, **fetch})


async def _run_until(pred, coro, stop, timeout=5.0, settle=0.0):
    task = asyncio.create_task(coro)
    try:
        for _ in range(int(timeout / 0.01)):
            if pred():
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(settle)  # let the last batch flush
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


# --- throttle ----------------------------------------------------------------


def test_throttle_samples_calmly_and_passes_jumps():
    th = Throttle(every=60, delta=0.003)
    o = lambda t, v: Observation("btc_usd", "GLOBAL", t, v)  # noqa: E731
    assert th.offer(o(0, 11.300)) is not None  # first always goes
    assert th.offer(o(10, 11.301)) is None  # routine drift: held
    assert th.offer(o(20, 11.302)) is None
    assert th.flush_due(59) == []
    assert [x.value for x in th.flush_due(61)] == [11.302]  # latest held value, on time
    assert th.offer(o(70, 11.310)) is not None  # a 0.8% jump: at once


# --- parsers ------------------------------------------------------------------


def test_coinbase_ticker_parser(sources):
    cfg = sources["btc_usd"]
    (obs,) = parsers.parse({"message": json.loads(_tick(83888.46)), "received": 0}, cfg)
    assert obs.value == math.log1p(83888.46) and obs.cell == "GLOBAL"
    assert parsers.parse({"message": json.loads(_tick(1, product="ETH-USD"))}, cfg) == []
    assert parsers.parse({"message": {"type": "subscriptions"}}, cfg) == []


def test_emsc_parser_keeps_region_and_event_page(sources):
    cfg = sources["emsc_seismic"]
    (obs,) = parsers.parse({"message": json.loads(_emsc())}, cfg)
    assert obs.value == 4.2 and obs.context["flynn_region"] == "WESTERN TURKEY"
    assert obs.context["url"].endswith("unid=20260926_0000078")
    low = dataclasses.replace(cfg, parse={**cfg.parse, "filter_min_mag": 5.0})
    assert parsers.parse({"message": json.loads(_emsc())}, low) == []


# --- the stream runner ----------------------------------------------------------


async def test_stream_stores_scores_and_heartbeats(db, sources):
    cfg = _fast(sources["btc_usd"], emit_every_seconds=0)  # no thinning: every tick
    stop = asyncio.Event()
    ws = FakeWS([_tick(84000, "2026-09-26T13:00:00Z"), _tick(84010, "2026-09-26T13:00:01Z")], stop)
    seen: list[Observation] = []

    async def on_new(c, obs, t):
        seen.extend(obs)

    await _run_until(lambda: len(seen) >= 2,
                     run_stream(db, cfg, on_new=on_new, stop=stop, connect=_connector([ws])), stop)
    assert ws.sent == [cfg.fetch["subscribe"]]
    assert [o.value for o in seen] == [math.log1p(84000), math.log1p(84010)]
    assert db.execute("SELECT COUNT(*) FROM raw_ring").fetchone()[0] == 2
    events = [r[0] for r in db.execute("SELECT event FROM health WHERE component = 'btc_usd'")]
    assert "connected" in events and "ok" in events  # presence sees a live feed


async def test_stream_reconnects_after_failure(db, sources):
    cfg = _fast(sources["emsc_seismic"])
    stop = asyncio.Event()
    ws = FakeWS([_emsc()], stop)
    got: list = []

    async def on_new(c, obs, t):
        got.extend(obs)

    connect = _connector([OSError("network unreachable"), ws])
    await _run_until(lambda: got, run_stream(db, cfg, on_new=on_new, stop=stop, connect=connect,
                                             min_backoff=0.01), stop)
    assert len(got) == 1
    err = db.execute("SELECT detail FROM health WHERE event = 'stream_error'").fetchone()
    assert "network unreachable" in err[0]


async def test_malformed_message_does_not_kill_the_stream(db, sources):
    cfg = _fast(sources["emsc_seismic"])
    stop = asyncio.Event()
    ws = FakeWS(["{not json", _emsc(unid="a", t="2026-09-26T09:00:00Z")], stop)
    got: list = []

    async def on_new(c, obs, t):
        got.extend(obs)

    await _run_until(lambda: got, run_stream(db, cfg, on_new=on_new, stop=stop, connect=_connector([ws])), stop)
    assert len(got) == 1
    assert db.execute("SELECT COUNT(*) FROM health WHERE event = 'parse_error'").fetchone()[0] == 1


async def test_emsc_updates_of_a_seen_event_are_not_recounted(db, sources):
    cfg = _fast(sources["emsc_seismic"])
    stop = asyncio.Event()
    ws = FakeWS([_emsc("create"), _emsc("update", mag=4.3)], stop)
    await _run_until(lambda: not ws.messages,
                     run_stream(db, cfg, stop=stop, connect=_connector([ws])), stop, settle=0.1)
    assert db.execute("SELECT COUNT(*) FROM raw_ring").fetchone()[0] == 1  # one quake, two messages


@pytest.mark.parametrize("sid", ["btc_usd", "eth_usd", "emsc_seismic"])
def test_push_stanzas_are_websocket(sources, sid):
    cfg = sources[sid]
    assert cfg.fetch["kind"] == "websocket" and cfg.endpoint.startswith("wss://")


async def test_silent_feed_is_reconnected_when_stale(db, sources):
    """A feed that should talk constantly (Coinbase heartbeat) and goes quiet
    is treated as a dead link: reconnect, recorded as data."""
    cfg = _fast(sources["btc_usd"], stale_seconds=0.05)
    stop = asyncio.Event()
    silent, alive = FakeWS([], stop), FakeWS([_tick(84000, "2026-09-26T13:00:00Z")], stop)
    got: list = []

    async def on_new(c, obs, t):
        got.extend(obs)

    await _run_until(lambda: got, run_stream(db, cfg, on_new=on_new, stop=stop,
                                             connect=_connector([silent, alive]), min_backoff=0.01), stop)
    err = db.execute("SELECT detail FROM health WHERE event = 'stream_error'").fetchone()
    assert "no message for" in err[0] and len(got) == 1


def test_coinbase_liveness_is_its_heartbeat(sources):
    f = sources["btc_usd"].fetch
    assert "heartbeat" in f["subscribe"]["channels"] and f["ping_interval"] == 0 and f["stale_seconds"] == 60
