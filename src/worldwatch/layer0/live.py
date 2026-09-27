"""Live scoring (ADR 0002 §A): score observations as they arrive.

Runs inside the poll process. Each newly ingested observation goes to its
(stream, cell) Layer-0 model at the stream's native resolution
(worldwatch.layer0.native), the surprise row is written to the archive at
scale NATIVE_SCALE, and the stream's CUSUM evidence is updated in memory, so
the alert policy can act within seconds of the poll:

  continuous  scored immediately, at observation time. An observation older
              than the model's last one is recorded (out of order), not scored.
  count       reports are counted into windows by ARRIVAL time; `tick` closes
              each window just after it ends (+ grace) and scores it for
              every active cell, zero counts included. A cell stays active
              for `active_seconds` after its last report.

Exactly-once: raw_ring.scored is set in the same transaction as the surprise
rows, model states and window counts. The consolidator folds only scored rows
of live streams, and `replay` scores whatever a crash left unscored.
"""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from worldwatch.alerts.engine import (
    DEFAULT_CUSUM_DRIFT,
    DEFAULT_CUSUM_H,
    DEFAULT_LOOKBACK_SECONDS,
    DEFAULT_RECENT_SECONDS,
    Anomaly,
    cusum_step,
    tail_extremity,
    tail_of,
)
from worldwatch.config.loader import SourceConfig
from worldwatch.ingest.models import Observation
from worldwatch.instrument import record_health
from worldwatch.layer0 import models
from worldwatch.layer0.models import SUPPORTED_FLAVORS, Layer0Model
from worldwatch.layer0.native import NATIVE_SCALE, native_seconds

DEFAULT_GRACE_SECONDS = 60
DEFAULT_ACTIVE_SECONDS = 30 * 86400
DEFAULT_MAX_CATCHUP_SECONDS = 86400


@dataclass
class _Series:
    s: float = 0.0  # CUSUM statistic
    last_bin: int = 0
    last_q: float = 0.5


class LiveScorer:
    def __init__(
        self,
        conn: sqlite3.Connection,
        sources: dict[str, SourceConfig],
        *,
        now: int | None = None,
        grace_seconds: int = DEFAULT_GRACE_SECONDS,
        active_seconds: int = DEFAULT_ACTIVE_SECONDS,
        max_catchup_seconds: int = DEFAULT_MAX_CATCHUP_SECONDS,
        h: float = DEFAULT_CUSUM_H,
        k: float = DEFAULT_CUSUM_DRIFT,
        recent_seconds: int = DEFAULT_RECENT_SECONDS,
    ) -> None:
        self.conn = conn
        self.sources = {
            sid: cfg for sid, cfg in sources.items()
            if cfg.status != "retired" and cfg.flavor in SUPPORTED_FLAVORS
        }
        self.grace = grace_seconds
        self.active = active_seconds
        self.max_catchup = max_catchup_seconds
        self.h, self.k, self.recent = h, k, recent_seconds
        self._models: dict[tuple[str, str], Layer0Model] = {}
        self._series: dict[tuple[str, str], _Series] = {}
        self._restore_cusum(now if now is not None else int(time.time()))

    @property
    def live_streams(self) -> set[str]:
        return set(self.sources)

    # --- ingestion ---------------------------------------------------------

    def ingest(self, cfg: SourceConfig, obs: Iterable[Observation], now: int) -> int:
        """Score (continuous) or count (count) newly ingested observations.
        Returns the surprise rows written."""
        return self._ingest(cfg, [(o, now) for o in obs])

    def replay(self, now: int) -> int:
        """Score observations a crash left unscored (raw_ring.scored = 0)."""
        rows = self.conn.execute(
            "SELECT r.stream_id, r.cell, r.ts, r.value, s.first_seen FROM raw_ring r "
            "JOIN seen s ON s.stream_id = r.stream_id AND s.cell = r.cell AND s.ts = r.ts "
            "WHERE r.scored = 0 ORDER BY r.ts"
        ).fetchall()
        by_stream: dict[str, list[tuple[Observation, int]]] = defaultdict(list)
        for r in rows:
            o = Observation(r["stream_id"], r["cell"], r["ts"], r["value"])
            by_stream[r["stream_id"]].append((o, r["first_seen"]))
        written = 0
        for sid, items in by_stream.items():
            if sid in self.sources:
                written += self._ingest(self.sources[sid], items)
        return written + self.tick(now)

    def _ingest(self, cfg: SourceConfig, items: list[tuple[Observation, int]]) -> int:
        if cfg.stream_id not in self.sources or not items:
            return 0
        if cfg.flavor == "count":
            return self._count_arrivals(cfg, items)
        return self._score_points(cfg, items)

    def _score_points(self, cfg: SourceConfig, items: list[tuple[Observation, int]]) -> int:
        sid = cfg.stream_id
        version = models.MODEL_VERSION[cfg.flavor]
        rows, touched, consumed, out_of_order = [], set(), [], 0
        for o, _ in sorted(items, key=lambda it: (it[0].cell, it[0].ts)):
            consumed.append((sid, o.cell, o.ts))
            if o.value is None:
                continue
            m = self._model(cfg, o.cell)
            last = getattr(m, "_last_ts", None)
            if last is not None and o.ts <= last:
                out_of_order += 1  # frozen archive: never re-scored
                continue
            q = m.update(o.ts, float(o.value))
            d = models.detection_q(m, q)
            rows.append((sid, o.cell, NATIVE_SCALE, o.ts, q, 1.0, 1.0, 1, None, version, d))
            touched.add(o.cell)
            self._observe(sid, o.cell, o.ts, q if d is None else d)
        self._commit(cfg, rows, touched, consumed, now=max(fs for _, fs in items))
        if out_of_order:
            record_health(self.conn, sid, "out_of_order", f"skipped={out_of_order}")
        return len(rows)

    def _count_arrivals(self, cfg: SourceConfig, items: list[tuple[Observation, int]]) -> int:
        sid, w = cfg.stream_id, native_seconds(cfg)
        windows: dict[tuple[str, int], int] = defaultdict(int)
        last_event: dict[str, int] = {}
        for o, first_seen in items:
            windows[(o.cell, (first_seen // w) * w)] += 1
            last_event[o.cell] = max(last_event.get(o.cell, 0), first_seen)
        self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.executemany(
                "INSERT INTO live_windows (stream_id, cell, win_start, n) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (stream_id, cell, win_start) DO UPDATE SET n = n + excluded.n",
                [(sid, cell, ws, n) for (cell, ws), n in windows.items()],
            )
            self.conn.executemany(
                "INSERT INTO live_cells (stream_id, cell, last_event_ts) VALUES (?, ?, ?) "
                "ON CONFLICT (stream_id, cell) DO UPDATE SET "
                "last_event_ts = max(last_event_ts, excluded.last_event_ts)",
                [(sid, cell, ts) for cell, ts in last_event.items()],
            )
            self.conn.executemany(
                "UPDATE raw_ring SET scored = 1 WHERE stream_id = ? AND cell = ? AND ts = ?",
                [(sid, o.cell, o.ts) for o, _ in items],
            )
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()
        return 0  # counts are scored when their window closes (tick)

    def register_cells(self, stream_id: str, cells: list[str], now: int) -> None:
        """Declare cells a count stream always covers (the prober's countries),
        so their quiet windows are scored as zeros from the start — a first
        outage then meets an informed model, not a blank one."""
        self.conn.executemany(
            "INSERT INTO live_cells (stream_id, cell, last_event_ts) VALUES (?, ?, ?) "
            "ON CONFLICT (stream_id, cell) DO UPDATE SET "
            "last_event_ts = max(last_event_ts, excluded.last_event_ts)",
            [(stream_id, c, now) for c in cells],
        )
        self.conn.commit()

    # --- closing count windows ------------------------------------------------

    def tick(self, now: int) -> int:
        """Close every count window that ended at least `grace` ago; score it
        (zeros included) for each active cell. Returns surprise rows written."""
        written = 0
        for cfg in self.sources.values():
            if cfg.flavor == "count":
                written += self._close_windows(cfg, now)
        return written

    def _close_windows(self, cfg: SourceConfig, now: int) -> int:
        sid, w = cfg.stream_id, native_seconds(cfg)
        last_due = ((now - self.grace - w) // w) * w  # newest window start that is closed
        counts = {
            (r["cell"], r["win_start"]): r["n"]
            for r in self.conn.execute(
                "SELECT cell, win_start, n FROM live_windows WHERE stream_id = ? AND win_start <= ?",
                (sid, last_due),
            )
        }
        cells = [
            r["cell"] for r in self.conn.execute(
                "SELECT cell FROM live_cells WHERE stream_id = ? AND last_event_ts >= ?",
                (sid, now - self.active),
            )
        ]
        version = models.MODEL_VERSION[cfg.flavor]
        floor = last_due - (self.max_catchup // w) * w
        rows, touched = [], set()
        # a cell's first window: its earliest pending one, closed or not (a cell
        # registered without any report yet starts at the newest closed window)
        first_window = {
            r["cell"]: r["w"] for r in self.conn.execute(
                "SELECT cell, MIN(win_start) AS w FROM live_windows WHERE stream_id = ? GROUP BY cell",
                (sid,),
            )
        }
        for cell in cells:
            m = self._model(cfg, cell)
            last = getattr(m, "_last_ts", None)
            start = last + w if last is not None else first_window.get(cell, last_due)
            for ws in range(max(start, floor), last_due + 1, w):
                q = m.update(ws, counts.get((cell, ws), 0))
                d = models.detection_q(m, q)
                rows.append((sid, cell, NATIVE_SCALE, ws, q, 1.0, 1.0,
                             counts.get((cell, ws), 0), None, version, d))
                touched.add(cell)
                self._observe(sid, cell, ws, q if d is None else d)
        if not rows and not counts:
            return 0
        self._commit(cfg, rows, touched, [], now=now,
                     extra_sql=[("DELETE FROM live_windows WHERE stream_id = ? AND win_start <= ?",
                                 (sid, last_due))])
        return len(rows)

    # --- candidates for the alert policy -------------------------------------

    def candidates(self, now: int) -> list[Anomaly]:
        out = []
        for (sid, cell), st in self._series.items():
            cfg = self.sources.get(sid)
            if cfg is None or st.s < self.h or st.last_bin < now - self.recent:
                continue
            out.append(Anomaly(
                stream_id=sid, cell=cell, scale=NATIVE_SCALE, bin_start=st.last_bin,
                q_value=st.last_q, presence_q=1.0, precision=1.0, modality=cfg.modality,
                extremity=tail_extremity(st.last_q, tail_of(cfg)), evidence=st.s,
                bin_seconds=native_seconds(cfg),
            ))
        return out

    # --- internals -------------------------------------------------------------

    def _observe(self, sid: str, cell: str, bin_start: int, q: float) -> None:
        st = self._series.setdefault((sid, cell), _Series())
        st.s = cusum_step(st.s, q, self.k, tail_of(self.sources.get(sid)))
        st.last_bin, st.last_q = bin_start, q

    def _restore_cusum(self, now: int) -> None:
        for r in self.conn.execute(
            "SELECT stream_id, cell, bin_start, COALESCE(q_detect, q_value) AS q_value FROM surprise "
            "WHERE scale = ? AND bin_start >= ? AND q_value IS NOT NULL "
            "ORDER BY stream_id, cell, bin_start",
            (NATIVE_SCALE, now - DEFAULT_LOOKBACK_SECONDS),
        ):
            if r["stream_id"] in self.sources:
                self._observe(r["stream_id"], r["cell"], r["bin_start"], r["q_value"])

    def _model(self, cfg: SourceConfig, cell: str) -> Layer0Model:
        key = (cfg.stream_id, cell)
        if key not in self._models:
            row = self.conn.execute(
                "SELECT state FROM model_state WHERE stream_id = ? AND cell = ? AND scale = ? "
                "AND version = ?",
                (cfg.stream_id, cell, NATIVE_SCALE, models.MODEL_VERSION[cfg.flavor]),
            ).fetchone()
            m = None
            if row is not None:
                try:
                    m = models.load_model(cfg.flavor, row["state"], key)
                except Exception as e:  # states are caches; the archive is the record
                    record_health(self.conn, cfg.stream_id, "model_reset", f"{cell}: {e}")
            self._models[key] = m if m is not None else models.make_model(cfg, cell)
        return self._models[key]

    def _commit(
        self,
        cfg: SourceConfig,
        rows: list[tuple],
        touched: set[str],
        consumed: list[tuple[str, str, int]],
        *,
        now: int,
        extra_sql: list[tuple[str, tuple]] | None = None,
    ) -> None:
        version = models.MODEL_VERSION[cfg.flavor]
        self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.executemany(
                "INSERT OR IGNORE INTO surprise (stream_id, cell, scale, bin_start, q_value, "
                "presence_q, precision, n_obs, tail_index, model_version, q_detect) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self.conn.executemany(
                "INSERT OR REPLACE INTO model_state (stream_id, cell, scale, version, state, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                [(cfg.stream_id, cell, NATIVE_SCALE, version,
                  self._models[(cfg.stream_id, cell)].to_bytes(), now) for cell in touched],
            )
            if consumed:
                self.conn.executemany(
                    "UPDATE raw_ring SET scored = 1 WHERE stream_id = ? AND cell = ? AND ts = ?",
                    consumed,
                )
            for sql, params in extra_sql or []:
                self.conn.execute(sql, params)
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()
