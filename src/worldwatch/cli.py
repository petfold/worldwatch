"""Worldwatch process entrypoints (invoked by systemd units/timers).

  init         register sources; create/migrate the database (run once)
  poll         long-running: one async poller per source, scoring new
               observations on arrival and alerting in-process (a service)
  consolidate  one cascade fold pass (a timer)
  detect       alert + notify sweep over the surprise archive (a timer; the
               safety net — scoring happens live in `poll`, ADR 0002)
  presence     one presence pass (a timer)
  digest       the weekly report: every significant deviation, an LLM's analysis,
               one silent push (a weekly timer; once per week)
  api          long-running: serve the dashboard + API (a service)

The pass commands are idempotent and crash-safe, so timers can fire them
repeatedly. All communicate only through the database (architecture §11).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sqlite3
import time

import httpx

from worldwatch import usage
from worldwatch.alerts.engine import open_alerts, run_alerts
from worldwatch.api.notify import notify_alerts
from worldwatch.cascade.consolidator import consolidate
from worldwatch.config.loader import SourceConfig
from worldwatch.instrument import record_health
from worldwatch.layer0.presence import run_presence
from worldwatch.layer0.live import LiveScorer
from worldwatch.layer0.nursery import run_nursery
from worldwatch.layer0.models import SUPPORTED_FLAVORS, prune_states
from worldwatch.poll.poller import run_poller
from worldwatch.poll.stream import run_stream
from worldwatch.probe.prober import run_prober
from worldwatch.store import upsert_source


def cmd_init(conn: sqlite3.Connection, sources: dict[str, SourceConfig]) -> int:
    """Register every configured source into the sources table."""
    for cfg in sources.values():
        upsert_source(
            conn,
            cfg.stream_id,
            {
                "class": cfg.class_,
                "modality": cfg.modality,
                "topic_tags": cfg.topic_tags,
                "flavor": cfg.flavor,
                "config": {"endpoint": cfg.endpoint, "cadence_seconds": cfg.cadence_seconds},
                "status": cfg.status,
            },
        )
    return len(sources)


def cmd_consolidate(
    conn: sqlite3.Connection,
    fine_window_seconds: int,
    seen_retention_seconds: int | None = None,
    context_budget_bytes: int | None = None,
    live_streams: set[str] | None = None,
) -> int:
    kwargs: dict = {}
    if seen_retention_seconds is not None:
        kwargs["seen_retention_seconds"] = seen_retention_seconds
    if context_budget_bytes is not None:
        kwargs["context_budget_bytes"] = context_budget_bytes
    if live_streams is not None:
        kwargs["live_streams"] = live_streams  # fold only what the live scorer consumed
    n = consolidate(conn, fine_window_seconds=fine_window_seconds, **kwargs)
    pruned = prune_states(conn)  # superseded model versions' states
    if pruned:
        record_health(conn, "model_state", "pruned", f"rows={pruned}")
    return n


def live_stream_ids(sources: dict[str, SourceConfig]) -> set[str]:
    return {sid for sid, c in sources.items() if c.status != "retired" and c.flavor in SUPPORTED_FLAVORS}


def cmd_digest(conn: sqlite3.Connection, sources: dict[str, SourceConfig]) -> int | None:
    from worldwatch.api.digest import run_digest

    return run_digest(conn, sources)


def cmd_presence(conn: sqlite3.Connection, sources: dict[str, SourceConfig]) -> int:
    return run_presence(conn, sources)


def cmd_detect(conn: sqlite3.Connection, sources: dict[str, SourceConfig]) -> dict[str, int]:
    """Safety-net sweep: apply the alert policy to the surprise archive and push
    newly opened alerts. Scoring itself happens live in the poll process
    (ADR 0002); this catches anything the live path missed, idempotently."""
    opened = run_alerts(conn, sources)
    delivered = asyncio.run(notify_alerts(conn, opened, sources=sources))
    return {"opened": len(opened), "notified": delivered}


TICK_SECONDS = 30


async def cmd_poll(conn: sqlite3.Connection, sources: dict[str, SourceConfig]) -> None:
    """Run one poller per (non-retired) source until cancelled, scoring new
    observations as they arrive and alerting within the same event loop."""
    active = [c for c in sources.values() if c.status != "retired"]
    now = int(time.time())
    record_health(conn, "poll", "start", f"sources={len(active)}", ts=now)
    live = LiveScorer(conn, sources, now=now)
    async with httpx.AsyncClient(transport=usage.CountingTransport()) as client:

        async def alert_and_notify(t: int) -> None:
            opened = open_alerts(conn, sources, live.candidates(t), t)
            if opened:
                await notify_alerts(conn, opened, client=client, sources=sources)

        async def on_new(cfg: SourceConfig, obs: list, t: int) -> None:
            live.ingest(cfg, obs, t)
            await alert_and_notify(t)

        async def ticker() -> None:
            usage.tag("live")  # pushes sent from here
            while True:
                t = int(time.time())
                try:
                    n = live.tick(t)
                    if n:
                        record_health(conn, "live", "ok", f"closed_windows={n}", ts=t)
                    await alert_and_notify(t)
                except Exception as e:  # never let the ticker die silently
                    record_health(conn, "live", "tick_error", f"{type(e).__name__}: {e}", ts=t)
                await asyncio.sleep(TICK_SECONDS)

        replayed = live.replay(now)
        if replayed:
            record_health(conn, "live", "replay", f"rows={replayed}", ts=now)
        async def resources() -> None:
            usage.tag("resources")
            from worldwatch.runtime import db_path

            while True:
                t = int(time.time())
                try:
                    usage.flush(conn, t)
                    usage.sample(conn, t, db_path())
                    await usage.check(conn, client, t)
                except Exception as e:  # never let the sampler die silently
                    record_health(conn, "resources", "sample_error", f"{type(e).__name__}: {e}", ts=t)
                await asyncio.sleep(usage.SAMPLE_SECONDS)

        async def runner(cfg: SourceConfig) -> None:  # push feeds stream, the prober probes, the rest poll
            usage.tag(cfg.stream_id)  # this task's traffic is charged to its source
            if cfg.fetch.get("kind") == "websocket":
                return await run_stream(conn, cfg, on_new=on_new)
            if cfg.fetch.get("kind") == "probe":
                return await run_prober(conn, cfg, client, on_new=on_new, register_cells=live.register_cells)
            return await run_poller(client, conn, cfg, on_new=on_new)

        await asyncio.gather(ticker(), resources(), *(runner(cfg) for cfg in active))


def cmd_export(conn: sqlite3.Connection) -> dict[str, int]:
    """The daily Parquet export of the permanent record; its outcome is recorded as data."""
    from worldwatch.export import export
    from worldwatch.runtime import db_path, export_dir

    try:
        counts = export(db_path(), export_dir())
    except Exception as e:
        record_health(conn, "export", "export_error", f"{type(e).__name__}: {e}")
        raise
    record_health(conn, "export", "ok", json.dumps(counts))
    return counts


def cmd_api() -> None:
    import uvicorn

    from worldwatch.api.app import create_app
    from worldwatch.config.loader import load_sources
    from worldwatch.runtime import config_dir, db_path

    host = os.environ.get("WW_API_HOST", "127.0.0.1")
    port = int(os.environ.get("WW_API_PORT", "8000"))
    uvicorn.run(create_app(db_path(), sources=load_sources(config_dir())), host=host, port=port)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="worldwatch")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "poll", "consolidate", "detect", "presence", "nursery", "digest", "export", "api"):
        sub.add_parser(name)
    args = parser.parse_args(argv)

    # `api` doesn't need the shared loader path (opens its own read connections).
    if args.command == "api":
        cmd_api()
        return 0

    from worldwatch.runtime import (
        context_budget_bytes,
        fine_window_seconds,
        load,
        seen_retention_seconds,
    )

    conn, sources = load()
    if args.command == "init":
        n = cmd_init(conn, sources)
        print(f"registered {n} sources")
        # judge calibration now, so a deploy's restart already alerts on the calibrated
        print(json.dumps(run_nursery(conn, sources)))
    elif args.command == "nursery":
        print(json.dumps(run_nursery(conn, sources)))
    elif args.command == "consolidate":
        n = cmd_consolidate(
            conn, fine_window_seconds(), seen_retention_seconds(), context_budget_bytes(),
            live_stream_ids(sources),
        )
        print(f"consolidated {n} rows")
    elif args.command == "presence":
        print(f"silence rows: {cmd_presence(conn, sources)}")
    elif args.command == "detect":
        print(json.dumps(cmd_detect(conn, sources)))
    elif args.command == "digest":
        week = cmd_digest(conn, sources)
        print(f"digest for the week to {week}" if week else "this week's digest exists")
    elif args.command == "export":
        print(json.dumps(cmd_export(conn)))
    elif args.command == "poll":
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(cmd_poll(conn, sources))
    return 0
