"""Async poller framework.

One asyncio task per source (per-source failure isolation, guardrail 3):
a broken API stalls only its own task. Every outcome — ok, 304, http_error,
timeout, parse_error — is recorded in the health table as data (P9), not just
logged. Schedule is jittered to avoid thundering-herd on shared endpoints,
unless a stanza's `phase_seconds` pins it to the clock (a feed that publishes
on the hour is read just after); transient failures back off exponentially up
to the source's cadence.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from worldwatch.config.loader import SourceConfig
from worldwatch.ingest import parsers
from worldwatch.ingest.models import Observation
from worldwatch.instrument import record_health
from worldwatch.poll.fetch import get_fetcher
from worldwatch.poll.http import CacheValidators
from worldwatch.store import write_new_observations

# Deterministic per-source jitter fraction of the cadence (no Date/random needed):
# hash the stream_id to a stable [0, 0.25) offset.
_MAX_JITTER_FRAC = 0.25


def jitter_seconds(stream_id: str, cadence: int) -> float:
    h = 0
    for ch in stream_id:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return (h % 1000) / 1000.0 * _MAX_JITTER_FRAC * cadence


def slot_delay(now: float, cadence: int, phase: int) -> float:
    """Seconds until the next UTC time t with t ≡ phase (mod cadence): with
    cadence 3600 and phase 240, four minutes past every hour."""
    return (phase - now) % cadence


@dataclass(slots=True)
class PollOutcome:
    event: str  # ok | not_modified | http_error | timeout | fetch_error | parse_error
    rows_written: int = 0
    detail: str | None = None


OnNew = Callable[[SourceConfig, list[Observation], int], Awaitable[None]]


async def poll_once(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    cfg: SourceConfig,
    validators: CacheValidators,
    *,
    now: int | None = None,
    on_new: OnNew | None = None,
) -> PollOutcome:
    """One fetch → parse → store cycle for a single source. Never raises;
    classifies failures into a PollOutcome and instruments them. `on_new`
    receives the newly stored observations (the live scorer, ADR 0002); its
    faults are recorded as data and never fail the poll."""
    poll_time = now if now is not None else int(time.time())
    try:
        fetcher = get_fetcher(cfg)
        result = await fetcher(client, cfg, validators, poll_time)
    except httpx.TimeoutException as e:
        record_health(conn, cfg.stream_id, "timeout", cfg.redact(str(e)), ts=poll_time)
        return PollOutcome("timeout", detail=cfg.redact(str(e)))
    except httpx.HTTPError as e:
        record_health(conn, cfg.stream_id, "http_error", cfg.redact(str(e)), ts=poll_time)
        return PollOutcome("http_error", detail=cfg.redact(str(e)))
    except Exception as e:  # isolation boundary: fetcher fault → data, not a crash
        record_health(conn, cfg.stream_id, "fetch_error", cfg.redact(f"{type(e).__name__}: {e}"), ts=poll_time)
        return PollOutcome("fetch_error", detail=cfg.redact(str(e)))

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
        record_health(conn, cfg.stream_id, "parse_error", cfg.redact(f"{type(e).__name__}: {e}"), ts=poll_time)
        return PollOutcome("parse_error", detail=cfg.redact(str(e)))

    new = write_new_observations(conn, obs, now=poll_time)
    record_health(conn, cfg.stream_id, "ok", f"rows={len(new)}", ts=poll_time)
    if on_new is not None and new:
        try:
            await on_new(cfg, new, poll_time)
        except Exception as e:  # isolation boundary: scoring fault → data, not a stalled poller
            record_health(conn, cfg.stream_id, "score_error", f"{type(e).__name__}: {e}", ts=poll_time)
    return PollOutcome("ok", rows_written=len(new))


def _stamp_now(obs: list[Observation], poll_time: int) -> list[Observation]:
    """Replace the parser's ts sentinel (-1) with the poll time for sources
    whose payloads carry no timestamp (e.g. spot prices)."""
    out = []
    for o in obs:
        out.append(o if o.ts != parsers._NOW_SENTINEL else _with_ts(o, poll_time))
    return out


def _with_ts(o: Observation, ts: int) -> Observation:
    return Observation(stream_id=o.stream_id, cell=o.cell, ts=ts, value=o.value, meta=o.meta)


def load_validators(conn: sqlite3.Connection, stream_id: str) -> CacheValidators:
    """The source's last conditional-request memo (ETag / Last-Modified, or a
    fetcher's own: the last granule, key or file), so a restart doesn't fetch
    again what is already stored."""
    row = conn.execute("SELECT etag, last_modified FROM poll_state WHERE stream_id = ?",
                       (stream_id,)).fetchone()
    return CacheValidators(etag=row[0], last_modified=row[1]) if row else CacheValidators()


def save_validators(conn: sqlite3.Connection, stream_id: str, validators: CacheValidators) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO poll_state (stream_id, etag, last_modified, updated_at) VALUES (?, ?, ?, ?)",
        (stream_id, validators.etag, validators.last_modified, int(time.time())))
    conn.commit()


async def run_poller(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    cfg: SourceConfig,
    *,
    stop: asyncio.Event | None = None,
    on_new: OnNew | None = None,
) -> None:
    """Long-running loop for one source: jittered cadence (or the stanza's
    slot on the clock), exponential backoff on transient failure (capped at
    the cadence)."""
    validators = load_validators(conn, cfg.stream_id)
    backoff = 0.0
    phase = cfg.extra.get("phase_seconds")
    # Stagger startup across sources, or wait for the source's slot.
    if phase is None:
        await asyncio.sleep(jitter_seconds(cfg.stream_id, cfg.cadence_seconds))
    else:
        await asyncio.sleep(slot_delay(time.time(), cfg.cadence_seconds, int(phase)))

    while stop is None or not stop.is_set():
        before = (validators.etag, validators.last_modified)
        outcome = await poll_once(client, conn, cfg, validators, on_new=on_new)
        if (validators.etag, validators.last_modified) != before:
            save_validators(conn, cfg.stream_id, validators)
        if outcome.event in ("timeout", "http_error", "fetch_error"):
            backoff = min(cfg.cadence_seconds, max(1.0, backoff * 2 or 1.0))
            sleep_for = backoff
        elif phase is None:
            backoff = 0.0
            sleep_for = cfg.cadence_seconds
        else:
            backoff = 0.0
            sleep_for = slot_delay(time.time(), cfg.cadence_seconds, int(phase))
            if sleep_for < 1.0:  # still inside the slot just polled
                sleep_for += cfg.cadence_seconds
        await asyncio.sleep(sleep_for)
