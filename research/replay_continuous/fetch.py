"""Fetch real histories for the continuous-model replay (public APIs, no keys).

  btc / eth : Coinbase Exchange 1-minute candles (close), the last 14 days
  rivers    : PEGELONLINE water level (cm), 30 gauges, the last 15 days, hourly
  elexon    : GB grid frequency (Hz), 15 s readings, the last 7 days, every 5 min
Writes data/<name>.csv as cell,ts,value. The data stay out of git.
"""
import csv
import sys
import time
from datetime import UTC, datetime

import httpx

UA = {"User-Agent": "worldwatch-research/0.1 (https://github.com/petfold/worldwatch)"}
NOW = int(time.time())


def write(name, rows):
    with open(f"data/{name}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cell", "ts", "value"])
        w.writerows(rows)
    print(name, len(rows), "rows", file=sys.stderr)


def coinbase(product):
    rows, end = [], NOW
    with httpx.Client(headers=UA, timeout=30) as c:
        while end > NOW - 14 * 86400:
            start = end - 300 * 60
            r = c.get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                      params={"granularity": 60, "start": datetime.fromtimestamp(start, UTC).isoformat(),
                              "end": datetime.fromtimestamp(end, UTC).isoformat()})
            r.raise_for_status()
            rows += [("GLOBAL", int(k[0]), float(k[4])) for k in r.json()]
            end = start
            time.sleep(0.35)  # public limit: a few requests a second
    return sorted(set(rows), key=lambda x: x[1])


def rivers(n=30):
    base = "https://www.pegelonline.wsv.de/webservices/rest-api/v2"
    rows = []
    with httpx.Client(headers=UA, timeout=30) as c:
        stations = c.get(f"{base}/stations.json", params={"timeseries": "W"}).json()
        for st in stations[:: max(1, len(stations) // n)][:n]:
            r = c.get(f"{base}/stations/{st['uuid']}/W/measurements.json", params={"start": "P15D"})
            if r.status_code != 200:
                continue
            last = None
            for m in r.json():
                ts = int(datetime.fromisoformat(m["timestamp"]).timestamp())
                if last is None or ts - last >= 3600:  # the poller reads it hourly
                    rows.append((st["number"], ts, float(m["value"])))
                    last = ts
            time.sleep(0.2)
    return rows


def elexon():
    rows = []
    with httpx.Client(headers=UA, timeout=60) as c:
        for d in range(7, 0, -1):
            a = datetime.fromtimestamp(NOW - d * 86400, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            b = datetime.fromtimestamp(NOW - (d - 1) * 86400, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            r = c.get("https://data.elexon.co.uk/bmrs/api/v1/system/frequency",
                      params={"from": a, "to": b, "format": "json"})
            r.raise_for_status()
            data = r.json()
            data = data.get("data", data)
            last = None
            for m in sorted(data, key=lambda m: m["measurementTime"]):
                ts = int(datetime.fromisoformat(m["measurementTime"].replace("Z", "+00:00")).timestamp())
                if last is None or ts - last >= 300:
                    rows.append(("GB", ts, float(m["frequency"])))
                    last = ts
    return rows


if __name__ == "__main__":
    import math
    write("btc", [(c, t, math.log1p(v)) for c, t, v in coinbase("BTC-USD")])
    write("eth", [(c, t, math.log1p(v)) for c, t, v in coinbase("ETH-USD")])
    write("rivers", rivers())
    write("elexon", elexon())


def swpc_xray():
    """GOES X-ray flux 0.1-0.8 nm, 1 min, the last 7 days (as the stanza: log flux)."""
    import math
    with httpx.Client(headers=UA, timeout=60) as c:
        x = c.get("https://services.swpc.noaa.gov/json/goes/primary/xrays-7-day.json").json()
    rows = [("GLOBAL", int(datetime.fromisoformat(r["time_tag"].replace("Z", "+00:00")).timestamp()),
             math.log(r["flux"])) for r in x if r.get("energy") == "0.1-0.8nm" and r.get("flux", 0) > 0]
    return sorted(rows, key=lambda r: r[1])
