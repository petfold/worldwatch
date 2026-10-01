"""Fetch the EMSC catalogue (M >= 1.0, as the emsc_seismic stanza filters) for a date range, from
its FDSN event service, in 3-day chunks, into a local cache (not committed). Written in the USGS
files' columns (time, latitude, longitude, depth, mag, place, id), so the replay scripts read
either catalogue.

    PYTHONPATH=src python research/replay_changepoint/fetch_emsc.py 2026-06-27 2026-09-27
"""

from __future__ import annotations

import csv
import io
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from worldwatch.poll.http import USER_AGENT

CACHE = Path.home() / ".cache" / "worldwatch-research" / "emsc"
URL = ("https://www.seismicportal.eu/fdsnws/event/1/query?format=text&orderby=time-asc&minmag=1.0"
       "&limit=20000&starttime={a}&endtime={b}")
COLUMNS = ["time", "latitude", "longitude", "depth", "mag", "place", "id"]


def fetch(a: datetime, b: datetime) -> list[list[str]]:
    url = URL.format(a=a.strftime("%Y-%m-%dT%H:%M:%S"), b=b.strftime("%Y-%m-%dT%H:%M:%S"))
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=120) as r:
        text = r.read().decode()
    rows = []
    for line in io.StringIO(text):
        if line.startswith("#") or not line.strip():
            continue
        f = line.rstrip("\n").split("|")  # EventID|Time|Latitude|Longitude|Depth/km|...|Magnitude|MagAuthor|EventLocationName
        rows.append([f[1], f[2], f[3], f[4], f[10], f[12], f[0]])
    return rows


def main(start: str, end: str) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    t = datetime.fromisoformat(start).replace(tzinfo=UTC)
    stop = datetime.fromisoformat(end).replace(tzinfo=UTC)
    step = timedelta(days=3)  # ~400 events a day at M1+: well under the 20,000 per request
    while t < stop:
        u = min(t + step, stop)
        out = CACHE / f"{t:%Y%m%d}_{u:%Y%m%d}.csv"
        if not out.exists():
            rows = fetch(t, u)
            if len(rows) >= 20000:
                raise SystemExit(f"{out.name}: {len(rows)} rows, at the limit: use a shorter step")
            with open(out, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(COLUMNS)
                w.writerows(rows)
            print(out.name, len(rows), flush=True)
            time.sleep(1.0)  # polite pacing (guardrail 9)
        t = u


if __name__ == "__main__":
    main(*sys.argv[1:3])
