"""Fetch GDELT 2.0's 15-minute export files for a date range (public, no auth) into a local
cache, and count each batch the way the gdelt_events stream does (its own parser: geocoded
events, one per distinct article per resolution-3 cell). Writes one row per (batch, cell), in
the catalogue files' columns plus a count n: time is the batch window's start, latitude and
longitude the cell's centre, so the replay scripts map it back to the same cell.

    PYTHONPATH=src python research/replay_changepoint/fetch_gdelt.py 2026-09-03 2026-10-01
"""

from __future__ import annotations

import csv
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import h3

from worldwatch.config.loader import load_sources
from worldwatch.ingest.parsers import parse_gdelt_export_events
from worldwatch.poll.http import USER_AGENT

CACHE = Path.home() / ".cache" / "worldwatch-research"
RAW, OUT = CACHE / "gdelt_raw", CACHE / "gdelt"
CONFIG = Path(__file__).resolve().parents[2] / "src" / "worldwatch" / "config" / "sources"
URL = "https://storage.googleapis.com/data.gdeltproject.org/gdeltv2/{stamp}.export.CSV.zip"
WINDOW = 900


def fetch(stamp: str) -> Path | None:
    path = RAW / f"{stamp}.export.CSV.zip"
    if path.exists():
        return path
    for attempt in range(3):
        try:
            req = urllib.request.Request(URL.format(stamp=stamp), headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as r:
                path.write_bytes(r.read())
            return path
        except urllib.error.HTTPError as e:
            if e.code == 404:  # GDELT skips a batch now and then
                return None
            time.sleep(2.0 * (attempt + 1))
        except OSError:
            time.sleep(2.0 * (attempt + 1))
    return None


def main(start: str, end: str) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = load_sources(CONFIG)["gdelt_events"]
    t = datetime.fromisoformat(start).replace(tzinfo=UTC)
    stop = datetime.fromisoformat(end).replace(tzinfo=UTC)
    day = t
    while day < stop:
        out = OUT / f"{day:%Y%m%d}.csv"
        nxt = min(day + timedelta(days=1), stop)
        if not out.exists():
            # a batch's file is stamped with its end; the day's windows start at day 00:00
            ends = [day + timedelta(seconds=WINDOW * (k + 1)) for k in range(int((nxt - day).total_seconds()) // WINDOW)]
            with ThreadPoolExecutor(4) as pool:  # four at a time: about one file a second each
                paths = list(pool.map(fetch, [f"{e:%Y%m%d%H%M%S}" for e in ends]))
            rows, missing = [], 0
            for e, path in zip(ends, paths, strict=True):
                if path is None:
                    missing += 1
                    continue
                payload = {"content": path.read_bytes(), "batch_epoch": int(e.timestamp())}
                counts: dict[str, int] = {}
                for o in parse_gdelt_export_events(payload, cfg):
                    counts[o.cell] = counts.get(o.cell, 0) + 1
                w0 = (e - timedelta(seconds=WINDOW)).strftime("%Y-%m-%dT%H:%M:%SZ")
                for cell, n in sorted(counts.items()):
                    lat, lng = h3.cell_to_latlng(cell)
                    rows.append([w0, f"{lat:.6f}", f"{lng:.6f}", "", "", cell, "", n])
            with open(out, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["time", "latitude", "longitude", "depth", "mag", "place", "id", "n"])
                w.writerows(rows)
            print(out.name, len(rows), "cell-batches,", sum(r[-1] for r in rows), "articles,", missing, "batches missing", flush=True)
        day = nxt


if __name__ == "__main__":
    main(*sys.argv[1:3])
