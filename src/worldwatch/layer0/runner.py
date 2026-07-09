"""Layer-0 runner: bins → per-(stream, cell, scale) models → surprise archive.

This is the connective tissue that turns the ingestion+cascade half of P0 into
a working pipeline. For each (stream, cell, scale) it:
  1. finds the cursor = max bin_start already scored (from the surprise table),
  2. loads the saved model_state or cold-starts from config,
  3. feeds each newer bin (in bin_start order) to the model → q_value,
  4. writes one surprise row per bin and persists the advanced model_state,
all in a single transaction per group so a crash leaves surprise and
model_state consistent (guardrail 7). Re-running is a no-op over already-scored
bins (idempotent, incremental).

The observation fed per bin is the bin's mean (continuous) or event count
(count). Within a scale, bins share a fixed width, so counts are comparable.

Presence, precision weighting, and GPD tail_index are later steps; for P0 the
runner writes presence_q = 1.0, precision = 1.0, tail_index = NULL, and scores
every non-retired source (nursery models write in shadow — they simply won't be
alert-eligible until step 8 gates on status).
"""

from __future__ import annotations

import sqlite3
import time

from worldwatch.config.loader import SourceConfig
from worldwatch.instrument import record_health
from worldwatch.layer0 import models
from worldwatch.layer0.models import SUPPORTED_FLAVORS, Layer0Model

# Placeholders until the corresponding steps land.
_PRESENCE_Q_PRESENT = 1.0
_PRECISION_DEFAULT = 1.0


def run_layer0(
    conn: sqlite3.Connection,
    sources: dict[str, SourceConfig],
    now: int | None = None,
) -> int:
    """Score all newly-consolidated bins. Returns the surprise rows written."""
    run_now = now if now is not None else int(time.time())
    total = 0
    for cfg in sources.values():
        if cfg.status == "retired" or cfg.flavor not in SUPPORTED_FLAVORS:
            continue
        total += _score_stream(conn, cfg, run_now)
    record_health(conn, "layer0", "ok", f"surprise_rows={total}", ts=run_now)
    return total


def _score_stream(conn: sqlite3.Connection, cfg: SourceConfig, run_now: int) -> int:
    # Distinct (cell, scale) groups that have bins for this stream.
    groups = conn.execute(
        "SELECT DISTINCT cell, scale FROM bins WHERE stream_id = ? ORDER BY cell, scale",
        (cfg.stream_id,),
    ).fetchall()

    written = 0
    for g in groups:
        written += _score_group(conn, cfg, g["cell"], g["scale"], run_now)
    return written


def _score_group(
    conn: sqlite3.Connection, cfg: SourceConfig, cell: str, scale: int, run_now: int
) -> int:
    cursor_row = conn.execute(
        "SELECT MAX(bin_start) AS c FROM surprise WHERE stream_id = ? AND cell = ? AND scale = ?",
        (cfg.stream_id, cell, scale),
    ).fetchone()
    cursor = cursor_row["c"]

    if cursor is None:
        new_bins = conn.execute(
            "SELECT bin_start, n, vmean FROM bins "
            "WHERE stream_id = ? AND cell = ? AND scale = ? ORDER BY bin_start",
            (cfg.stream_id, cell, scale),
        ).fetchall()
        model = models.make_model(cfg)
    else:
        new_bins = conn.execute(
            "SELECT bin_start, n, vmean FROM bins "
            "WHERE stream_id = ? AND cell = ? AND scale = ? AND bin_start > ? "
            "ORDER BY bin_start",
            (cfg.stream_id, cell, scale, cursor),
        ).fetchall()
        model = _load_group_model(conn, cfg, cell, scale)

    if not new_bins:
        return 0

    version = models.MODEL_VERSION[cfg.flavor]
    surprise_rows = []
    for b in new_bins:
        value = _observation(cfg.flavor, b["n"], b["vmean"])
        if value is None:
            continue  # continuous bin with no numeric value — cannot score
        q = model.update(b["bin_start"], value)
        surprise_rows.append(
            (
                cfg.stream_id,
                cell,
                scale,
                b["bin_start"],
                q,
                _PRESENCE_Q_PRESENT,
                _PRECISION_DEFAULT,
                b["n"],
                None,  # tail_index (GPD) — deferred to P1
                version,
            )
        )

    if not surprise_rows:
        return 0

    conn.execute("BEGIN")
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO surprise "
            "(stream_id, cell, scale, bin_start, q_value, presence_q, precision, "
            " n_obs, tail_index, model_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            surprise_rows,
        )
        conn.execute(
            "INSERT OR REPLACE INTO model_state "
            "(stream_id, cell, scale, version, state, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (cfg.stream_id, cell, scale, version, model.to_bytes(), run_now),
        )
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return len(surprise_rows)


def _load_group_model(
    conn: sqlite3.Connection, cfg: SourceConfig, cell: str, scale: int
) -> Layer0Model:
    row = conn.execute(
        "SELECT state FROM model_state "
        "WHERE stream_id = ? AND cell = ? AND scale = ? ORDER BY version DESC LIMIT 1",
        (cfg.stream_id, cell, scale),
    ).fetchone()
    if row is None:
        return models.make_model(cfg)
    return models.load_model(cfg.flavor, row["state"])


def _observation(flavor: str, n: int, vmean: float | None) -> float | None:
    if flavor == "count":
        return float(n)
    return None if vmean is None else float(vmean)
