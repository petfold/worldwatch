"""USGS catalogue -> Worldwatch's usgs_seismic streams: counts per 5-minute window (the
stanza's native_seconds) for the world and for the busiest H3 resolution-3 cells (its
geocode), and the larger quakes (M >= 5), saved to the cache for replay.py."""

from __future__ import annotations

import csv
import glob
from datetime import datetime
from pathlib import Path

import h3
import numpy as np

CACHE = Path.home() / ".cache" / "worldwatch-research"
W = 300


def main(n_cells: int = 12) -> None:
    rows = []
    for f in sorted(glob.glob(str(CACHE / "usgs" / "*.csv"))):
        for r in csv.DictReader(open(f)):
            ts = datetime.fromisoformat(r["time"].replace("Z", "+00:00")).timestamp()
            rows.append((ts, float(r["latitude"]), float(r["longitude"]), float(r["mag"]), r["place"]))
    rows.sort()
    ts = np.array([r[0] for r in rows])
    cells = np.array([h3.latlng_to_cell(r[1], r[2], 3) for r in rows])
    t0 = int(ts.min() // W * W)
    n = int((ts.max() - t0) // W) + 1
    win = ((ts - t0) // W).astype(int)
    names, counts = ["world"], [np.bincount(win, minlength=n)]
    top = [c for c, _ in zip(*np.unique(cells, return_counts=True))]
    uc, cc = np.unique(cells, return_counts=True)
    for c in uc[np.argsort(cc)[::-1][:n_cells]]:
        names.append(c)
        counts.append(np.bincount(win[cells == c], minlength=n))
    big = [(r[0], c, r[3], r[4]) for r, c in zip(rows, cells) if r[3] >= 5.0]
    np.savez(CACHE / "usgs_streams.npz", t0=t0, width=W, names=np.array(names), counts=np.array(counts),
             big_ts=np.array([b[0] for b in big]), big_cell=np.array([b[1] for b in big]),
             big_mag=np.array([b[2] for b in big]), big_place=np.array([b[3] for b in big]))
    print(f"{len(rows)} events, {n} windows of {W} s ({n * W / 86400:.0f} days)")
    for nm, c in zip(names, counts):
        print(f"  {nm}: {c.sum()} events, mean {c.mean():.3f} per window, max {c.max()}, "
              f"index of dispersion {c.var() / c.mean():.1f}")
    in_top = [b for b in big if b[1] in names]
    print(f"M>=5: {len(big)} quakes, {len(in_top)} in the streams' cells:")
    for b in in_top[:15]:
        print(f"  {datetime.utcfromtimestamp(b[0]):%Y-%m-%d %H:%M} M{b[2]:.1f} {b[1]} {b[3]}")


if __name__ == "__main__":
    main()
