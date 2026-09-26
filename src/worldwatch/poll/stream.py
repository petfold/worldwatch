"""Push feeds over WebSocket (ADR 0002 §D): observations as the source sends them.

A stanza with `[fetch] kind = "websocket"` is run by `run_stream` instead of
the polling loop: connect, send the stanza's `subscribe` message (if any),
parse each message with the stanza's parser, and store and live-score new
observations in small batches (every `flush_seconds`). The rest of the
pipeline is unchanged — the same parsers, `seen` dedup, evidence store and
live scorer.

- **Throttling.** A firehose (Coinbase sends ~8 BTC ticks a second) is
  thinned per cell: emit at most every `emit_every_seconds`, but at once
  when the value has moved by more than `emit_delta` since the last emitted
  one. Routine drift is sampled calmly; a jump goes out immediately.
- **Presence.** While connected, an "ok" health row is written once per
  `cadence_seconds` (with message/row counts), so a quiet-but-alive feed
  (EMSC between quakes) reads as reporting and a dropped connection as
  silence.
- **Isolation.** Every failure is recorded as data; the stream reconnects with
  exponential backoff (capped) and never affects other sources.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable
from typing import Any

from worldwatch.config.loader import SourceConfig
from worldwatch.ingest import parsers
from worldwatch.ingest.models import Observation
from worldwatch.instrument import record_health
from worldwatch.poll.http import USER_AGENT
from worldwatch.store import write_new_observations

OnNew = Callable[[SourceConfig, list[Observation], int], Awaitable[None]]
MAX_BACKOFF_SECONDS = 300.0


class Throttle:
    """Per-cell thinning: emit every `every` seconds, or at once on a move
    larger than `delta`; between emissions only the latest value is kept."""

    def __init__(self, every: float, delta: float | None) -> None:
        self.every, self.delta = every, delta
        self._last: dict[str, Observation] = {}
        self._pending: dict[str, Observation] = {}

    def offer(self, o: Observation) -> Observation | None:
        last = self._last.get(o.cell)
        due = last is None or o.ts - last.ts >= self.every
        jump = (
            self.delta is not None and last is not None and o.value is not None
            and last.value is not None and abs(o.value - last.value) >= self.delta
        )
        if due or jump:
            self._last[o.cell] = o
            self._pending.pop(o.cell, None)
            return o
        self._pending[o.cell] = o
        return None

    def flush_due(self, now: float) -> list[Observation]:
        """Latest held values whose interval has elapsed (a quiet market still
        reports every `every` seconds)."""
        out = []
        for cell, o in list(self._pending.items()):
            last = self._last.get(cell)
            if last is None or now - last.ts >= self.every:
                self._last[cell] = o
                del self._pending[cell]
                out.append(o)
        return out


async def run_stream(
    conn: sqlite3.Connection,
    cfg: SourceConfig,
    *,
    on_new: OnNew | None = None,
    stop: asyncio.Event | None = None,
    connect: Callable[..., Any] | None = None,
    clock: Callable[[], float] = time.time,
    min_backoff: float = 1.0,
) -> None:
    """Long-running loop for one push feed. Never raises (until cancelled)."""
    if connect is None:
        import websockets

        connect = websockets.connect
    every = cfg.fetch.get("emit_every_seconds")
    throttle = (
        Throttle(float(every), cfg.fetch.get("emit_delta")) if every is not None else None
    )
    flush_seconds = float(cfg.fetch.get("flush_seconds", 1.0))
    backoff = 0.0
    while stop is None or not stop.is_set():
        try:
            async with connect(
                cfg.endpoint, user_agent_header=USER_AGENT, open_timeout=30, ping_interval=20
            ) as ws:
                if cfg.fetch.get("subscribe"):
                    await ws.send(json.dumps(cfg.fetch["subscribe"]))
                record_health(conn, cfg.stream_id, "connected", ts=int(clock()))
                backoff = 0.0
                await _pump(conn, cfg, ws, throttle, flush_seconds, on_new, stop, clock)
            reason = "closed by peer"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # isolation boundary: stream fault → data, then reconnect
            reason = f"{type(e).__name__}: {e}"
        if stop is not None and stop.is_set():
            return
        record_health(conn, cfg.stream_id, "stream_error", reason[:300], ts=int(clock()))
        backoff = min(MAX_BACKOFF_SECONDS, max(min_backoff, backoff * 2 or min_backoff))
        await asyncio.sleep(backoff)


async def _pump(
    conn: sqlite3.Connection,
    cfg: SourceConfig,
    ws: Any,
    throttle: Throttle | None,
    flush_seconds: float,
    on_new: OnNew | None,
    stop: asyncio.Event | None,
    clock: Callable[[], float],
) -> None:
    batch: list[Observation] = []
    messages = rows = parse_errors = 0
    next_flush = clock() + flush_seconds
    next_beat = clock()  # heartbeat right away, then once per cadence
    while stop is None or not stop.is_set():
        timeout = max(0.05, min(next_flush, next_beat) - clock())
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout)
        except asyncio.TimeoutError:
            raw = None
        now = clock()
        if raw is not None:
            messages += 1
            try:
                for o in parsers.parse({"message": json.loads(raw), "received": int(now)}, cfg):
                    o = o if o.ts != parsers._NOW_SENTINEL else _with_ts(o, int(now))
                    kept = throttle.offer(o) if throttle else o
                    if kept is not None:
                        batch.append(kept)
            except Exception as e:  # a malformed message never kills the stream
                parse_errors += 1
                if parse_errors <= 3:
                    record_health(conn, cfg.stream_id, "parse_error",
                                  f"{type(e).__name__}: {e}"[:300], ts=int(now))
        if now >= next_flush:
            if throttle:
                batch.extend(throttle.flush_due(now))
            if batch:
                new = write_new_observations(conn, batch, now=int(now))
                rows += len(new)
                batch = []
                if on_new is not None and new:
                    try:
                        await on_new(cfg, new, int(now))
                    except Exception as e:  # scoring fault → data, stream continues
                        record_health(conn, cfg.stream_id, "score_error",
                                      f"{type(e).__name__}: {e}"[:300], ts=int(now))
            next_flush = now + flush_seconds
        if now >= next_beat:
            record_health(conn, cfg.stream_id, "ok", f"messages={messages} rows={rows}", ts=int(now))
            messages = rows = 0
            next_beat = now + cfg.cadence_seconds


def _with_ts(o: Observation, ts: int) -> Observation:
    return Observation(o.stream_id, o.cell, ts, o.value, o.meta, o.context)
