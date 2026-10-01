"""The USGS catalogue through LiveScorer itself, pooled (ADR 0005) and unpooled.

A week of events (2026-08-29 to 2026-09-05, the Alaska swarm inside it) is written and ingested
as the poller would, arrival = event time, and a tick closes every 5-minute window. Each run uses
its own database in the cache directory. Reports the time per tick, the rows and alarms, and the
Alaska cell's first alarms.

    PYTHONPATH=src .venv/bin/python research/replay_changepoint/live_replay.py pooled
    PYTHONPATH=src .venv/bin/python research/replay_changepoint/live_replay.py unpooled
"""

from __future__ import annotations

import csv
import dataclasses
import glob
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import h3

from worldwatch.config.loader import load_sources
from worldwatch.db import open_db
from worldwatch.ingest.models import Observation
from worldwatch.layer0.live import LiveScorer
from worldwatch.store import write_new_observations

CACHE = Path.home() / ".cache" / "worldwatch-research"
CONFIG = Path(__file__).resolve().parents[2] / "src" / "worldwatch" / "config" / "sources"
START = int(datetime(2026, 8, 29, tzinfo=UTC).timestamp())
END = int(datetime(2026, 9, 5, tzinfo=UTC).timestamp())
ALASKA = "8322c4fffffffff"
W = 300


def main(kind: str) -> None:
    sources = load_sources(CONFIG)
    cfg = sources["usgs_seismic"]
    model = dict(cfg.extra.get("model", {}))
    if kind == "unpooled":
        model.pop("pool", None)
    cfg = dataclasses.replace(cfg, extra=dict(cfg.extra, model=model))
    sources = {"usgs_seismic": cfg}
    path = CACHE / f"live_replay_{kind}.db"
    path.unlink(missing_ok=True)
    conn = open_db(path)
    events = []
    for f in sorted(glob.glob(str(CACHE / "usgs" / "*.csv"))):
        with open(f) as fh:
            for r in csv.DictReader(fh):
                ts = int(datetime.fromisoformat(r["time"].replace("Z", "+00:00")).timestamp())
                if START <= ts < END:
                    cell = h3.latlng_to_cell(float(r["latitude"]), float(r["longitude"]), 3)
                    events.append((ts, cell, float(r["mag"] or 0.0)))
    events.sort()
    live = LiveScorer(conn, sources, now=START, grace_seconds=60)
    i, ticks, tick_s = 0, 0, 0.0
    for t in range(START + W, END + W, W):
        batch = []
        while i < len(events) and events[i][0] < t:
            ts, cell, mag = events[i]
            batch.append(Observation("usgs_seismic", cell, ts, mag))
            i += 1
        if batch:
            live.ingest(cfg, write_new_observations(conn, batch, now=t - 1), t - 1)
        tic = time.perf_counter()
        live.tick(t + 61)
        tick_s += time.perf_counter() - tic
        ticks += 1
    n_rows = conn.execute("SELECT COUNT(*) FROM surprise").fetchone()[0]
    cells = conn.execute("SELECT COUNT(DISTINCT cell) FROM surprise").fetchone()[0]
    alarms = conn.execute("SELECT COUNT(*) FROM surprise WHERE COALESCE(q_detect, q_value) >= 0.999").fetchone()[0]
    print(f"{kind}: {len(events)} events, {ticks} ticks, {tick_s / ticks * 1e3:.0f} ms per tick on average; "
          f"{n_rows} rows over {cells} cells, {alarms} alarms ({alarms / 7:.1f} a day)")
    rows = conn.execute("SELECT bin_start FROM surprise WHERE cell = ? AND COALESCE(q_detect, q_value) >= 0.999 "
                        "ORDER BY bin_start", (ALASKA,)).fetchall()
    when = [datetime.fromtimestamp(r[0], UTC).strftime("%m-%d %H:%M") for r in rows]
    print(f"  Alaska cell alarms: {len(when)}, first ones {when[:4]}")
    versions = [r[0] for r in conn.execute("SELECT DISTINCT model_version FROM surprise")]
    print(f"  model versions in the archive: {versions}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "pooled")
