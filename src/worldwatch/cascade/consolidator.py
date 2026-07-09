"""Consolidator: fold aged raw_ring rows into the geometric bin cascade.

Rows younger than the fine window stay in raw_ring at full resolution; rows
older than it are folded into their age-appropriate (scale, bin_start) bin —
carrying count/min/max/mean/M2 (Welford) plus a t-digest sketch — then deleted
from raw_ring (schema: "fine window only; pruned by consolidator").

Idempotent and crash-safe (guardrail 7): fold+delete run in one transaction and
processed rows are removed, so a re-run over the same window is a no-op.
Commutative: Welford/t-digest merges are order-independent, and raw_ring dedups
on its PK, so out-of-order or duplicate observations converge to the same bins.

Per-bin value moments assume a stream is consistently valued or pure-event
(its flavor/parse is fixed): bins.n counts observations, and vmean/m2/sketch
summarize whatever numeric values were present (NULL for pure-event bins).
"""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict

from worldwatch.cascade import welford
from worldwatch.cascade.bins import bin_for
from worldwatch.cascade.tdigest import TDigest
from worldwatch.cascade.welford import Moments

# Default fine-window retention for raw_ring before consolidation (48 h).
DEFAULT_FINE_WINDOW_SECONDS = 48 * 3600


def consolidate(
    conn: sqlite3.Connection,
    now: int | None = None,
    fine_window_seconds: int = DEFAULT_FINE_WINDOW_SECONDS,
) -> int:
    """Fold raw_ring rows older than the fine window into bins.

    Returns the number of raw rows consolidated (0 if none were due).
    """
    poll_now = now if now is not None else int(time.time())
    cutoff = poll_now - fine_window_seconds

    raw = conn.execute(
        "SELECT stream_id, cell, ts, value FROM raw_ring WHERE ts < ?",
        (cutoff,),
    ).fetchall()
    if not raw:
        return 0

    # Group aged observations by their target bin.
    grouped: dict[tuple[str, str, int, int], list[float | None]] = defaultdict(list)
    processed_pks: list[tuple[str, str, int]] = []
    for row in raw:
        stream_id, cell, ts, value = row["stream_id"], row["cell"], row["ts"], row["value"]
        scale, bin_start = bin_for(ts, poll_now)
        grouped[(stream_id, cell, scale, bin_start)].append(value)
        processed_pks.append((stream_id, cell, ts))

    conn.execute("BEGIN")
    try:
        for (stream_id, cell, scale, bin_start), values in grouped.items():
            _fold_group(conn, stream_id, cell, scale, bin_start, values)
        conn.executemany(
            "DELETE FROM raw_ring WHERE stream_id = ? AND cell = ? AND ts = ?",
            processed_pks,
        )
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return len(processed_pks)


def _fold_group(
    conn: sqlite3.Connection,
    stream_id: str,
    cell: str,
    scale: int,
    bin_start: int,
    values: list[float | None],
) -> None:
    n_obs = len(values)  # every observation counts, valued or not (count signal)
    present = [v for v in values if v is not None]
    batch_moments = welford.from_values(present)
    batch_digest = TDigest.from_values(present)

    existing = conn.execute(
        "SELECT n, vmin, vmax, vmean, m2, sketch FROM bins "
        "WHERE stream_id = ? AND cell = ? AND scale = ? AND bin_start = ?",
        (stream_id, cell, scale, bin_start),
    ).fetchone()

    if existing is not None:
        prior_moments = _moments_from_row(existing)
        merged = welford.merge(prior_moments, batch_moments)
        digest = TDigest.from_bytes(existing["sketch"])
        digest.merge(batch_digest)
        total_n = existing["n"] + n_obs
    else:
        merged = batch_moments
        digest = batch_digest
        total_n = n_obs

    has_values = merged.n > 0
    conn.execute(
        """INSERT INTO bins (stream_id, cell, scale, bin_start, n, vmin, vmax, vmean, m2, sketch)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(stream_id, cell, scale, bin_start) DO UPDATE SET
                n=excluded.n, vmin=excluded.vmin, vmax=excluded.vmax,
                vmean=excluded.vmean, m2=excluded.m2, sketch=excluded.sketch""",
        (
            stream_id,
            cell,
            scale,
            bin_start,
            total_n,
            merged.vmin if has_values else None,
            merged.vmax if has_values else None,
            merged.mean if has_values else None,
            merged.m2 if has_values else None,
            digest.to_bytes() if has_values else None,
        ),
    )


def _moments_from_row(row: sqlite3.Row) -> Moments:
    """Reconstruct a Moments accumulator from a stored bin row.

    Uses the stored observation count `n` as the value count; valid under the
    per-stream valued/pure-event consistency assumption (see module docstring).
    """
    if row["vmean"] is None:
        return Moments()  # pure-event bin: no value stats to carry
    import math

    return Moments(
        n=row["n"],
        mean=row["vmean"],
        m2=row["m2"] if row["m2"] is not None else 0.0,
        vmin=row["vmin"] if row["vmin"] is not None else math.inf,
        vmax=row["vmax"] if row["vmax"] is not None else -math.inf,
    )
