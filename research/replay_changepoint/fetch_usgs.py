"""Fetch the USGS catalogue (M >= 1.0, as the usgs_seismic stanza filters) for a date range,
in chunks under the FDSN limit, into a local cache (not committed).

    python research/replay_changepoint/fetch_usgs.py 2026-06-27 2026-09-27
"""

from __future__ import annotations

import csv
import io
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from worldwatch.poll.http import USER_AGENT

CACHE = Path.home() / ".cache" / "worldwatch-research" / "usgs"
URL = ("https://earthquake.usgs.gov/fdsnws/event/1/query?format=csv&orderby=time-asc"
       "&minmagnitude=1.0&starttime={a}&endtime={b}")


def fetch(a: datetime, b: datetime) -> str:
    req = urllib.request.Request(URL.format(a=a.strftime("%Y-%m-%dT%H:%M:%S"), b=b.strftime("%Y-%m-%dT%H:%M:%S")), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read().decode()


def main(start: str, end: str) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    t = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    stop = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
    step = timedelta(days=3)  # ~2,500 events a day worldwide at M1+: well under 20,000 per request
    while t < stop:
        u = min(t + step, stop)
        out = CACHE / f"{t:%Y%m%d}_{u:%Y%m%d}.csv"
        if not out.exists():
            text = fetch(t, u)
            rows = sum(1 for _ in csv.reader(io.StringIO(text))) - 1
            if rows >= 20000:
                raise SystemExit(f"{out.name}: {rows} rows, at the FDSN limit: use a shorter step")
            out.write_text(text)
            print(out.name, rows, flush=True)
            time.sleep(1.0)  # polite pacing (guardrail 9)
        t = u


if __name__ == "__main__":
    main(*sys.argv[1:3])
