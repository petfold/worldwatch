"""The USGS catalogue through LiveScorer itself, pooled (ADR 0005) and unpooled.

A week of events (2026-08-29 to 2026-09-05, the Alaska swarm inside it) is written and ingested
as the poller would, arrival = event time, and a tick closes every 5-minute window. Each run uses
its own database in the cache directory. Reports the time per tick, the rows and alarms, and the
Alaska cell's first alarms.

    PYTHONPATH=src .venv/bin/python research/replay_changepoint/live_replay.py pooled
    PYTHONPATH=src .venv/bin/python research/replay_changepoint/live_replay.py unpooled

Another stream and span (emsc_* streams read the EMSC catalogue of fetch_emsc.py, gdelt_events
the news counts of fetch_gdelt.py):
`live_replay.py pooled usgs_m45 2026-06-29 2026-09-27` replays the
M4.5+ detection stream (the catalogue's events at M >= 4.5) with pooling switched on for it, and
reports the candidates too (q_detect >= 0.9975: one reading crosses the alert engine's CUSUM at
h = 4, k = 2) and how each M >= 6 quake scored in its own cell and window.
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


def main(kind: str, stream: str = "usgs_seismic", start: int = START, end: int = END) -> None:
    sources = load_sources(CONFIG)
    cfg = sources[stream]
    model = dict(cfg.extra.get("model", {}))
    if kind == "unpooled":
        model.pop("pool", None)
    else:
        model["pool"] = "h3"
    cfg = dataclasses.replace(cfg, extra=dict(cfg.extra, model=model))
    sources = {stream: cfg}
    min_mag = 4.5 if stream.endswith("_m45") else float(cfg.extra.get("parse", {}).get("filter_min_mag", 0.0))
    tag = kind if stream == "usgs_seismic" and (start, end) == (START, END) else f"{stream}_{kind}"
    path = CACHE / f"live_replay_{tag}.db"
    path.unlink(missing_ok=True)
    conn = open_db(path)
    events = []
    # fetch_emsc.py, fetch_gdelt.py (counts per batch and cell), fetch_usgs.py
    catalogue = "emsc" if stream.startswith("emsc") else "gdelt" if stream.startswith("gdelt") else "usgs"
    for f in sorted(glob.glob(str(CACHE / catalogue / "*.csv"))):
        with open(f) as fh:
            for r in csv.DictReader(fh):
                ts = int(datetime.fromisoformat(r["time"].replace("Z", "+00:00")).timestamp())
                if start <= ts < end and float(r["mag"] or 0.0) >= min_mag:
                    cell = h3.latlng_to_cell(float(r["latitude"]), float(r["longitude"]), 3)
                    n = int(r.get("n") or 1)  # a batch's articles, spread over its window as the parser does
                    events += [(ts + (i * 900) // n, cell, float(r["mag"] or 0.0)) for i in range(n)]
    events.sort()
    live = LiveScorer(conn, sources, now=start, grace_seconds=60)
    i, ticks, tick_s = 0, 0, 0.0
    for t in range(start + W, end + W, W):
        batch = []
        while i < len(events) and events[i][0] < t:
            ts, cell, mag = events[i]
            batch.append(Observation(stream, cell, ts, mag))
            i += 1
        if batch:
            live.ingest(cfg, write_new_observations(conn, batch, now=t - 1), t - 1)
        tic = time.perf_counter()
        live.tick(t + 61)
        tick_s += time.perf_counter() - tic
        ticks += 1
    days = (end - start) / 86400
    n_rows = conn.execute("SELECT COUNT(*) FROM surprise").fetchone()[0]
    cells = conn.execute("SELECT COUNT(DISTINCT cell) FROM surprise").fetchone()[0]
    alarms = conn.execute("SELECT COUNT(*) FROM surprise WHERE COALESCE(q_detect, q_value) >= 0.999").fetchone()[0]
    print(f"{stream} {kind}: {len(events)} events, {ticks} ticks, {tick_s / ticks * 1e3:.0f} ms per tick on average; "
          f"{n_rows} rows over {cells} cells, {alarms} alarms ({alarms / days:.1f} a day)")
    tail = conn.execute("SELECT AVG(q_value > 0.99), AVG(q_value > 0.999), "
                        "SUM(COALESCE(q_detect, q_value) >= 0.9975) FROM surprise").fetchone()
    print(f"  P(q > 0.99) {tail[0]:.5f}, P(q > 0.999) {tail[1]:.5f}; candidates (q_detect >= 0.9975) {tail[2]} "
          f"({tail[2] / days:.1f} a day)")
    if stream == "usgs_seismic":
        rows = conn.execute("SELECT bin_start FROM surprise WHERE cell = ? AND COALESCE(q_detect, q_value) >= 0.999 "
                            "ORDER BY bin_start", (ALASKA,)).fetchall()
        when = [datetime.fromtimestamp(r[0], UTC).strftime("%m-%d %H:%M") for r in rows]
        print(f"  Alaska cell alarms: {len(when)}, first ones {when[:4]}")
    hits, big = 0, [e for e in events if e[2] >= 6.0]
    for ts, cell, mag in big:
        r = conn.execute("SELECT MAX(COALESCE(q_detect, q_value)) FROM surprise WHERE cell = ? AND bin_start = ?",
                         (cell, ts // W * W)).fetchone()[0]
        hits += r is not None and r >= 0.9975
        print(f"  M{mag:.1f} {datetime.fromtimestamp(ts, UTC):%m-%d %H:%M} {cell}: q_detect "
              f"{'none' if r is None else f'{r:.6f}'}")
    if big:
        print(f"  M >= 6 quakes that are candidates in their own cell and window: {hits} of {len(big)}")
    versions = [r[0] for r in conn.execute("SELECT DISTINCT model_version FROM surprise")]
    print(f"  model versions in the archive: {versions}")


def _day(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp())


if __name__ == "__main__":
    args = sys.argv[1:]
    main(args[0] if args else "pooled", *(args[1:2]),
         *((_day(args[2]), _day(args[3])) if len(args) >= 4 else ()))
