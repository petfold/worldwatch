"""Two live replays (live_replay.py's databases) over the span both have scored: the upper tail,
the candidates and alarms, first reports (q = 0.5), and the candidates by how active the cell's
resolution-1 region was over the span (its events, all magnitudes in the stream).

    .venv/bin/python research/replay_changepoint/live_compare.py usgs_m45      # pooled against unpooled
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import h3

CACHE = Path.home() / ".cache" / "worldwatch-research"
CANDIDATE = 0.9975  # one reading crosses the alert engine's CUSUM (h = 4, k = 2): p = 1 - q <= e^-6


def main(stream: str) -> None:
    dbs = {k: sqlite3.connect(CACHE / f"live_replay_{stream}_{k}.db") for k in ("unpooled", "pooled")}
    start, end = (max(c.execute("SELECT MIN(bin_start) FROM surprise").fetchone()[0] for c in dbs.values()),
                  min(c.execute("SELECT MAX(bin_start) FROM surprise").fetchone()[0] for c in dbs.values()))
    days = (end - start) / 86400
    span = (start, end)
    print(f"{stream}: {datetime.fromtimestamp(start, UTC):%Y-%m-%d} to {datetime.fromtimestamp(end, UTC):%Y-%m-%d %H:%M} "
          f"({days:.1f} days)\n")
    # activity of each resolution-1 region: reports (windows with events) in the pooled run's rows
    rows = dbs["pooled"].execute("SELECT cell, SUM(n_obs) FROM surprise WHERE bin_start BETWEEN ? AND ? "
                                 "GROUP BY cell", span).fetchall()
    region: dict[str, int] = {}
    for cell, n in rows:
        r1 = h3.cell_to_parent(cell, 1)
        region[r1] = region.get(r1, 0) + int(n or 0)
    print("| run | cell-windows | P(q>0.99) | P(q>0.999) | first reports (q = 0.5) | candidates/day | alarms/day |")
    print("|---|---|---|---|---|---|---|")
    cand = {}
    for k, c in dbs.items():
        n, q99, q999, half, nc, na = c.execute(
            "SELECT COUNT(*), AVG(q_value > 0.99), AVG(q_value > 0.999), SUM(q_value = 0.5), "
            "SUM(COALESCE(q_detect, q_value) >= ?), SUM(COALESCE(q_detect, q_value) >= 0.999) "
            "FROM surprise WHERE bin_start BETWEEN ? AND ?", (CANDIDATE, *span)).fetchone()
        print(f"| {k} | {n:,} | {q99:.4f} | {q999:.5f} | {half:,} | {nc / days:.1f} | {na / days:.1f} |")
        cand[k] = c.execute("SELECT cell, bin_start FROM surprise WHERE bin_start BETWEEN ? AND ? "
                            "AND COALESCE(q_detect, q_value) >= ?", (*span, CANDIDATE)).fetchall()
    # windows with a report, and whether they are candidates, by the activity of their region
    reports = dbs["pooled"].execute("SELECT cell, bin_start FROM surprise WHERE bin_start BETWEEN ? AND ? "
                                    "AND n_obs > 0", span).fetchall()
    bands = [(1, 1, "1 (the report alone)"), (2, 5, "2-5"), (6, 30, "6-30"), (31, 10**9, "31 or more")]
    print("\nReports (windows with an event) that are candidates, by their resolution-1 region's reports "
          "over the span:\n")
    print("| region's reports | report windows | candidates unpooled | candidates pooled |")
    print("|---|---|---|---|")
    sets = {k: set(v) for k, v in cand.items()}
    for lo, hi, label in bands:
        sel = [(c, b) for c, b in reports if lo <= region[h3.cell_to_parent(c, 1)] <= hi]
        if not sel:
            continue
        u = sum((c, b) in sets["unpooled"] for c, b in sel)
        p = sum((c, b) in sets["pooled"] for c, b in sel)
        print(f"| {label} | {len(sel):,} | {u:,} ({u / len(sel):.0%}) | {p:,} ({p / len(sel):.0%}) |")
    quiet = sum(1 for c, b in cand["pooled"] if (c, b) not in set(reports))
    print(f"\nPooled candidates in windows without a report: {quiet:,} (unpooled: "
          f"{sum(1 for c, b in cand['unpooled'] if (c, b) not in set(reports)):,})")
    big = []  # the M >= 6 quakes in the span (the seismic catalogues' files)
    catalogue = CACHE / ("emsc" if stream.startswith("emsc") else "usgs")
    for f in sorted(catalogue.glob("*.csv")):
        with open(f) as fh:
            for r in csv.DictReader(fh):
                ts = int(datetime.fromisoformat(r["time"].replace("Z", "+00:00")).timestamp())
                if start <= ts // 300 * 300 <= end and float(r["mag"] or 0.0) >= 6.0:
                    big.append((h3.latlng_to_cell(float(r["latitude"]), float(r["longitude"]), 3), ts // 300 * 300))
    if big:
        hits = {k: sum(cb in sets[k] for cb in big) for k in sets}
        print(f"M >= 6 quakes that are candidates in their own cell and window: unpooled {hits['unpooled']} of "
              f"{len(big)}, pooled {hits['pooled']} of {len(big)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "usgs_m45")
