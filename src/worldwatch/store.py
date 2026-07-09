"""Writes normalized observations into raw_ring. Idempotent on the PK."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from worldwatch.ingest.models import Observation


def write_observations(conn: sqlite3.Connection, obs: Iterable[Observation]) -> int:
    """Insert observations into raw_ring, ignoring duplicates on
    (stream_id, cell, ts). Returns the number of rows actually inserted.
    """
    rows = [
        (
            o.stream_id,
            o.cell,
            o.ts,
            o.value,
            json.dumps(o.meta, separators=(",", ":")) if o.meta else None,
        )
        for o in obs
    ]
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO raw_ring (stream_id, cell, ts, value, meta) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn.total_changes - before


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
