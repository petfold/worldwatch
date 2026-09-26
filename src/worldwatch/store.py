"""Writes normalized observations into raw_ring, once per observation key.

Idempotent across re-fetches: a key (stream_id, cell, ts) already recorded in
`seen` is dropped, even after its raw row has been consolidated away."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from typing import Any

from worldwatch.ingest.models import Observation


def write_observations(
    conn: sqlite3.Connection, obs: Iterable[Observation], now: int | None = None
) -> int:
    """Insert observations never seen before into raw_ring. Returns the number
    of rows actually inserted (re-fetched observations count as 0).
    """
    first_seen = now if now is not None else int(time.time())
    written = 0
    for o in obs:
        cur = conn.execute(
            "INSERT OR IGNORE INTO seen (stream_id, cell, ts, first_seen) VALUES (?, ?, ?, ?)",
            (o.stream_id, o.cell, o.ts, first_seen),
        )
        if cur.rowcount != 1:
            continue  # already ingested (possibly already folded into bins)
        conn.execute(
            "INSERT OR IGNORE INTO raw_ring (stream_id, cell, ts, value, meta) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                o.stream_id,
                o.cell,
                o.ts,
                o.value,
                json.dumps(o.meta, separators=(",", ":")) if o.meta else None,
            ),
        )
        written += 1
    conn.commit()
    return written


def upsert_source(conn: sqlite3.Connection, stream_id: str, cfg_dict: dict[str, Any]) -> None:
    """Register a source row from its config (idempotent)."""
    import time

    conn.execute(
        """INSERT INTO sources (stream_id, class, modality, topic_tags, flavor,
                                config, status, created_at)
           VALUES (:stream_id, :class, :modality, :topic_tags, :flavor,
                   :config, :status, :created_at)
           ON CONFLICT(stream_id) DO UPDATE SET
                class=excluded.class, modality=excluded.modality,
                topic_tags=excluded.topic_tags, flavor=excluded.flavor,
                config=excluded.config""",
        {
            "stream_id": stream_id,
            "class": cfg_dict["class"],
            "modality": cfg_dict["modality"],
            "topic_tags": json.dumps(cfg_dict.get("topic_tags", [])),
            "flavor": cfg_dict["flavor"],
            "config": json.dumps(cfg_dict.get("config", {})),
            "status": cfg_dict.get("status", "nursery"),
            "created_at": int(time.time()),
        },
    )
    conn.commit()
