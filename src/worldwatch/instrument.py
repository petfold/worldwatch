"""Self-instrumentation: poller/component health recorded as data (P9).

Health events are a first-class stream, not just logs — so that
"the world went quiet" is distinguishable from "the system went deaf".
"""

from __future__ import annotations

import sqlite3
import time


def record_health(
    conn: sqlite3.Connection,
    component: str,
    event: str,
    detail: str | None = None,
    ts: int | None = None,
) -> None:
    """Insert a health row. `event` is one of ok|http_error|timeout|parse_error|...

    Idempotent on (component, ts, event): a duplicate within the same second is
    dropped rather than raising, so crash-restart replays are safe.
    """
    conn.execute(
        "INSERT OR IGNORE INTO health (component, ts, event, detail) VALUES (?, ?, ?, ?)",
        (component, ts if ts is not None else int(time.time()), event, detail),
    )
    conn.commit()
