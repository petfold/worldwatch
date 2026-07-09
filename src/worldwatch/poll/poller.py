"""Async poller framework.

One asyncio task per source (per-source failure isolation, guardrail 3):
a broken API stalls only its own task. Every outcome — ok, 304, http_error,
timeout, parse_error — is recorded in the health table as data (P9), not just
logged. Schedule is jittered to avoid thundering-herd on shared endpoints;
transient failures back off exponentially up to the source's cadence.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass

import httpx

from worldwatch.config.loader import SourceConfig
from worldwatch.ingest import parsers
from worldwatch.ingest.models import Observation
from worldwatch.instrument import record_health
from worldwatch.poll.http import CacheValidators, conditional_get
from worldwatch.store import write_observations

# Deterministic per-source jitter fraction of the cadence (no Date/random needed):
# hash the stream_id to a stable [0, 0.25) offset.
_MAX_JITTER_FRAC = 0.25


def jitter_seconds(stream_id: str, cadence: int) -> float:
    h = 0
    for ch in stream_id:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return (h % 1000) / 1000.0 * _MAX_JITTER_FRAC * cadence


@dataclass(slots=True)
class PollOutcome:
    event: str  # ok | not_modified | http_error | timeout | parse_error
    rows_written: int = 0
    detail: str | None = None


async def poll_once(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    cfg: SourceConfig,
    validators: CacheValidators,
    *,
    now: int | None = None,
) -> PollOutcome:
    """One fetch → parse → store cycle for a single source. Never raises;
    classifies failures into a PollOutcome and instruments them."""
    poll_time = now if now is not None else int(time.time())
    try:
        result = await conditional_get(client, cfg.endpoint, validators)
    except httpx.TimeoutException as e:
        record_health(conn, cfg.stream_id, "timeout", str(e), ts=poll_time)
        return PollOutcome("timeout", detail=str(e))
    except httpx.HTTPError as e:
        record_health(conn, cfg.stream_id, "http_error", str(e), ts=poll_time)
        return PollOutcome("http_error", detail=str(e))

    # Carry updated validators back to the caller's state.
    validators.etag = result.validators.etag
    validators.last_modified = result.validators.last_modified

    if result.not_modified:
        record_health(conn, cfg.stream_id, "not_modified", ts=poll_time)
        return PollOutcome("not_modified")

    try:
        obs = parsers.parse(result.payload, cfg)
        obs = _stamp_now(obs, poll_time)
    except Exception as e:  # isolation boundary: any parser fault → data, not a crash
        record_health(conn, cfg.stream_id, "parse_error", f"{type(e).__name__}: {e}", ts=poll_time)
        return PollOutcome("parse_error", detail=str(e))

    written = write_observations(conn, obs)
    record_health(conn, cfg.stream_id, "ok", f"rows={written}", ts=poll_time)
    return PollOutcome("ok", rows_written=written)


def _stamp_now(obs: list[Observation], poll_time: int) -> list[Observation]:
    """Replace the parser's ts sentinel (-1) with the poll time for sources
    whose payloads carry no timestamp (e.g. spot prices)."""
    out = []
    for o in obs:
        out.append(o if o.ts != parsers._NOW_SENTINEL else _with_ts(o, poll_time))
    return out


def _with_ts(o: Observation, ts: int) -> Observation:
    return Observation(stream_id=o.stream_id, cell=o.cell, ts=ts, value=o.value, meta=o.meta)


async def run_poller(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    cfg: SourceConfig,
    *,
    stop: asyncio.Event | None = None,
) -> None:
    """Long-running loop for one source: jittered cadence, exponential backoff
    on transient failure (capped at the cadence)."""
    validators = CacheValidators()
    backoff = 0.0
    # Stagger startup across sources.
    await asyncio.sleep(jitter_seconds(cfg.stream_id, cfg.cadence_seconds))

    while stop is None or not stop.is_set():
        outcome = await poll_once(client, conn, cfg, validators)
        if outcome.event in ("timeout", "http_error"):
            backoff = min(cfg.cadence_seconds, max(1.0, backoff * 2 or 1.0))
            sleep_for = backoff
        else:
            backoff = 0.0
            sleep_for = cfg.cadence_seconds
        await asyncio.sleep(sleep_for)
