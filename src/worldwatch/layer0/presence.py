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
from collections import defaultdict
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

# Common-mode guard (P9): if a large fraction of independently-polled sources go
# absent in the same window, that is our internet/DNS/host failing — not the
# world. Such windows are tagged and their silence is suppressed (treated like
# system-deaf), rather than flagging a planet-wide event. The active prober is
# the principled cause-attribution fix (P1); this is the cheap global guard.
DEFAULT_COMMON_MODE_WINDOW = 3600
DEFAULT_COMMON_MODE_MIN_SOURCES = 3
DEFAULT_COMMON_MODE_FRACTION = 0.5


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
    common_mode_window: int = DEFAULT_COMMON_MODE_WINDOW,
    common_mode_min_sources: int = DEFAULT_COMMON_MODE_MIN_SOURCES,
    common_mode_fraction: float = DEFAULT_COMMON_MODE_FRACTION,
) -> int:
    """Walk each source's expected reporting slots (from the health table),
    update its presence model, and archive surprising silence rows. Returns the
    number of silence rows written. Incremental and restart-safe via a per-source
    slot cursor in presence_state.

    A common-mode guard (P9) first identifies windows in which many sources were
    simultaneously absent (our-side failure) and suppresses their silence.
    """
    run_now = now if now is not None else int(time.time())
    active = [cfg for cfg in sources.values() if cfg.status != "retired"]

    # Resolve each source's model + starting slot up front (needed to bound the
    # common-mode scan and to avoid re-reading presence_state).
    plans: list[tuple[SourceConfig, PresenceModel, int]] = []
    min_start: int | None = None
    for cfg in active:
        loaded = _load_presence(conn, cfg)
        if loaded is None:
            continue
        model, start_slot = loaded
        plans.append((cfg, model, start_slot))
        min_start = start_slot if min_start is None else min(min_start, start_slot)

    flagged: set[int] = set()
    if min_start is not None and len(plans) >= common_mode_min_sources:
        flagged = _common_mode_windows(
            conn,
            [cfg.stream_id for cfg, _, _ in plans],
            min_start,
            run_now,
            common_mode_window,
            common_mode_min_sources,
            common_mode_fraction,
        )
        for w in sorted(flagged):
            record_health(conn, "presence", "common_mode_fault", f"window_start={w}", ts=w)

    total = 0
    for cfg, model, start_slot in plans:
        total += _presence_stream(
            conn, cfg, model, start_slot, run_now, silence_q_threshold, flagged, common_mode_window
        )
    record_health(conn, "presence", "ok", f"silence_rows={total}", ts=run_now)
    return total


def _load_presence(conn: sqlite3.Connection, cfg: SourceConfig) -> tuple[PresenceModel, int] | None:
    """Return (model, start_slot) for a source, or None if it has never polled."""
    cadence = cfg.cadence_seconds
    state_row = conn.execute(
        "SELECT state, last_slot FROM presence_state WHERE stream_id = ?",
        (cfg.stream_id,),
    ).fetchone()
    if state_row is not None:
        return PresenceModel.from_bytes(state_row["state"]), state_row["last_slot"] + cadence
    first = conn.execute(
        "SELECT MIN(ts) AS t FROM health WHERE component = ?", (cfg.stream_id,)
    ).fetchone()["t"]
    if first is None:
        return None
    return PresenceModel(), (first // cadence) * cadence


def _common_mode_windows(
    conn: sqlite3.Connection,
    stream_ids: list[str],
    range_start: int,
    now: int,
    window: int,
    min_sources: int,
    fraction: float,
) -> set[int]:
    """Windows in which >= `fraction` of the sources that were polled came back
    absent (only failures) — the signature of an our-side outage. Only complete
    windows (fully in the past) are considered."""
    if not stream_ids:
        return set()
    placeholders = ",".join("?" * len(stream_ids))
    rows = conn.execute(
        f"SELECT component, ts, event FROM health "
        f"WHERE ts >= ? AND ts < ? AND component IN ({placeholders})",
        (range_start, now, *stream_ids),
    ).fetchall()
    polled: dict[int, set[str]] = defaultdict(set)
    present: dict[int, set[str]] = defaultdict(set)
    for r in rows:
        w = (r["ts"] // window) * window
        polled[w].add(r["component"])
        if r["event"] in _PRESENT_EVENTS:
            present[w].add(r["component"])

    flagged = set()
    for w, pol in polled.items():
        if w + window > now:
            continue  # incomplete window
        n_polled = len(pol)
        n_absent = n_polled - len(present.get(w, set()))
        if n_polled >= min_sources and n_absent / n_polled >= fraction:
            flagged.add(w)
    return flagged


def _presence_stream(
    conn: sqlite3.Connection,
    cfg: SourceConfig,
    model: PresenceModel,
    start_slot: int,
    run_now: int,
    threshold: float,
    common_mode_windows: set[int],
    common_mode_window: int,
) -> int:
    cadence = cfg.cadence_seconds

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
        if (
            not reported
            and (slot // common_mode_window) * common_mode_window in common_mode_windows
        ):
            slot += cadence
            continue  # our-side common-mode outage → don't blame or learn from it
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
