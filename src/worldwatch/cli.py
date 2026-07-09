"""Worldwatch process entrypoints (invoked by systemd units/timers).

  init         register sources; create/migrate the database (run once)
  poll         long-running: one async poller per source (a service)
  consolidate  one cascade fold pass (a timer)
  detect       one Layer-0 + alert + notify pass (a timer)
  presence     one presence pass (a timer)
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

from worldwatch.alerts.engine import run_alerts
from worldwatch.api.notify import notify_alerts
from worldwatch.cascade.consolidator import consolidate
from worldwatch.config.loader import SourceConfig
from worldwatch.instrument import record_health
from worldwatch.layer0.presence import run_presence
from worldwatch.layer0.runner import run_layer0
from worldwatch.poll.poller import run_poller
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


def cmd_consolidate(conn: sqlite3.Connection, fine_window_seconds: int) -> int:
    return consolidate(conn, fine_window_seconds=fine_window_seconds)


def cmd_presence(conn: sqlite3.Connection, sources: dict[str, SourceConfig]) -> int:
    return run_presence(conn, sources)


def cmd_detect(conn: sqlite3.Connection, sources: dict[str, SourceConfig]) -> dict[str, int]:
    """Score new bins → open corroborated alerts → push newly opened ones."""
    n_surprise = run_layer0(conn, sources)
    opened = run_alerts(conn, sources)
    delivered = asyncio.run(notify_alerts(conn, opened))
    return {"surprise": n_surprise, "opened": len(opened), "notified": delivered}


async def cmd_poll(conn: sqlite3.Connection, sources: dict[str, SourceConfig]) -> None:
    """Run one poller per (non-retired) source until cancelled."""
    active = [c for c in sources.values() if c.status != "retired"]
    record_health(conn, "poll", "start", f"sources={len(active)}", ts=int(time.time()))
    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(run_poller(client, conn, cfg) for cfg in active))


def cmd_api() -> None:
    import uvicorn

    from worldwatch.api.app import create_app
    from worldwatch.runtime import db_path

    host = os.environ.get("WW_API_HOST", "127.0.0.1")
    port = int(os.environ.get("WW_API_PORT", "8000"))
    uvicorn.run(create_app(db_path()), host=host, port=port)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="worldwatch")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "poll", "consolidate", "detect", "presence", "api"):
        sub.add_parser(name)
    args = parser.parse_args(argv)

    # `api` doesn't need the shared loader path (opens its own read connections).
    if args.command == "api":
        cmd_api()
        return 0

    from worldwatch.runtime import fine_window_seconds, load

    conn, sources = load()
    if args.command == "init":
        n = cmd_init(conn, sources)
        print(f"registered {n} sources")
    elif args.command == "consolidate":
        print(f"consolidated {cmd_consolidate(conn, fine_window_seconds())} rows")
    elif args.command == "presence":
        print(f"silence rows: {cmd_presence(conn, sources)}")
    elif args.command == "detect":
        print(json.dumps(cmd_detect(conn, sources)))
    elif args.command == "poll":
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(cmd_poll(conn, sources))
    return 0
