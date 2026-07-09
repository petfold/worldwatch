"""Presence channel — absence is data (P5).

A companion model per source of "did it report when expected". It learns the
normal reporting rhythm (nightly gaps, maintenance windows) so that expected
silence is unsurprising while a normally-reliable source going quiet is loud.
Missingness is never imputed; it is modeled like any other stream.

Model: per hour-of-day, an EWMA of the reporting probability p_hour[h]. For a
run of consecutive silent slots the surprise is the model's probability of
staying silent that long — Π(1 − p) over the run — so:

    presence_q = 1 − Π_run (1 − p)   for a silent slot   (→ 1 when silence is
                                                            very unlikely)
    presence_q = 0                   for a slot that reported

A nightly gap drives p_hour→0 at those hours, so log(1−p)→0 and silence there
stays near q=0. A reliable source (p→1) that misses even one slot lands in the
tail. This is a silence-run tail measure, not a strict PIT; presence PIT-audit
and cause attribution (poller vs network vs station) are P1 (architecture §4).
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass, field

import numpy as np

from worldwatch.config.loader import SourceConfig
from worldwatch.instrument import record_health
from worldwatch.layer0.count import hour_of_day

PRESENCE_MODEL_VERSION = 1

_EPS = 1e-4

# Non-spatial sentinel cell for source-level silence rows in the surprise archive.
PRESENCE_CELL = "_PRESENCE_"
# Health events meaning the source responded (vs failures = poller/network fault).
_PRESENT_EVENTS = frozenset({"ok", "not_modified"})
# Only silence at least this surprising is archived (expected gaps score ~0).
DEFAULT_SILENCE_Q_THRESHOLD = 0.5


@dataclass
class PresenceModel:
    """Per-source expected-reporting model."""

    lr: float = 0.05  # EWMA rate for the hourly reporting probability
    _p_hour: np.ndarray = field(default_factory=lambda: np.full(24, 0.5), repr=False)
    _silence_logsurv: float = 0.0  # log Π(1−p) over the current silence run

    def reporting_prob(self, slot_ts: int) -> float:
        return float(np.clip(self._p_hour[hour_of_day(slot_ts)], _EPS, 1.0 - _EPS))

    def observe(self, slot_ts: int, reported: bool) -> float:
        """Record one expected slot; return its presence_q, then update state."""
        p = self.reporting_prob(slot_ts)
        if reported:
            self._silence_logsurv = 0.0
            q = 0.0
        else:
            self._silence_logsurv += math.log1p(-p)  # += log(1 − p)
            q = 1.0 - math.exp(self._silence_logsurv)
        h = hour_of_day(slot_ts)
        self._p_hour[h] += self.lr * (float(reported) - self._p_hour[h])
        return max(0.0, min(1.0, q))

    # --- serialization ---

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "v": PRESENCE_MODEL_VERSION,
                "lr": self.lr,
                "p_hour": self._p_hour.tolist(),
                "silence_logsurv": self._silence_logsurv,
            },
            separators=(",", ":"),
        ).encode()

    @classmethod
    def from_bytes(cls, blob: bytes) -> PresenceModel:
        p = json.loads(blob)
        m = cls(lr=p["lr"])
        m._p_hour = np.array(p["p_hour"])
        m._silence_logsurv = p["silence_logsurv"]
        return m


def run_presence(
    conn: sqlite3.Connection,
    sources: dict[str, SourceConfig],
    now: int | None = None,
    silence_q_threshold: float = DEFAULT_SILENCE_Q_THRESHOLD,
) -> int:
    """Walk each source's expected reporting slots (from the health table),
    update its presence model, and archive surprising silence rows. Returns the
    number of silence rows written. Incremental and restart-safe via a per-source
    slot cursor in presence_state.
    """
    run_now = now if now is not None else int(time.time())
    total = 0
    for cfg in sources.values():
        if cfg.status == "retired":
            continue
        total += _presence_stream(conn, cfg, run_now, silence_q_threshold)
    record_health(conn, "presence", "ok", f"silence_rows={total}", ts=run_now)
    return total


def _presence_stream(
    conn: sqlite3.Connection, cfg: SourceConfig, run_now: int, threshold: float
) -> int:
    cadence = cfg.cadence_seconds
    state_row = conn.execute(
        "SELECT state, last_slot FROM presence_state WHERE stream_id = ?",
        (cfg.stream_id,),
    ).fetchone()

    if state_row is not None:
        model = PresenceModel.from_bytes(state_row["state"])
        start_slot = state_row["last_slot"] + cadence
    else:
        model = PresenceModel()
        first = conn.execute(
            "SELECT MIN(ts) AS t FROM health WHERE component = ?", (cfg.stream_id,)
        ).fetchone()["t"]
        if first is None:
            return 0  # source has never been polled
        start_slot = (first // cadence) * cadence

    # Only score slots whose window has fully elapsed.
    if start_slot + cadence > run_now:
        return 0

    events = conn.execute(
        "SELECT ts, event FROM health WHERE component = ? AND ts >= ? AND ts < ?",
        (cfg.stream_id, start_slot, run_now),
    ).fetchall()
    buckets: dict[int, set[str]] = {}
    for e in events:
        slot = (e["ts"] // cadence) * cadence
        buckets.setdefault(slot, set()).add(e["event"])

    silence_rows = []
    last_processed = None
    slot = start_slot
    while slot + cadence <= run_now:
        evs = buckets.get(slot)
        last_processed = slot
        if evs is None:
            slot += cadence
            continue  # poller didn't run this slot → system-deaf, not source-silent
        reported = bool(evs & _PRESENT_EVENTS)
        q = model.observe(slot, reported)
        if not reported and q >= threshold:
            silence_rows.append(
                (
                    cfg.stream_id,
                    PRESENCE_CELL,
                    0,
                    slot,
                    None,  # q_value: no observation
                    q,
                    1.0,
                    0,
                    None,
                    PRESENCE_MODEL_VERSION,
                )
            )
        slot += cadence

    if last_processed is None:
        return 0

    conn.execute("BEGIN")
    try:
        if silence_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO surprise "
                "(stream_id, cell, scale, bin_start, q_value, presence_q, precision, "
                " n_obs, tail_index, model_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                silence_rows,
            )
        conn.execute(
            "INSERT OR REPLACE INTO presence_state "
            "(stream_id, state, last_slot, updated_at, version) VALUES (?, ?, ?, ?, ?)",
            (cfg.stream_id, model.to_bytes(), last_processed, run_now, PRESENCE_MODEL_VERSION),
        )
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return len(silence_rows)
