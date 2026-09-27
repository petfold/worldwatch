#!/usr/bin/env python3
"""Build src/worldwatch/config/places.json.gz: country and sea outlines for naming places.

Natural Earth (public domain, naturalearthdata.com): 1:50m admin-0 countries and
1:10m marine areas (oceans, seas, gulfs, bays, straits); coordinates in hundredths
of a degree, thinned to one point per STEP° (~10 km), gzipped, so the file stays
small: enough to name the country or sea of a place, not to draw a border.

    tools/build_places.py
"""

from __future__ import annotations

import gzip
import json
import urllib.request
from pathlib import Path

BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
STEP = 0.1
PLAIN = {"Dem. Rep. Korea": "North Korea", "Republic of Korea": "South Korea", "Russian Federation": "Russia",
         "Lao PDR": "Laos", "Brunei Darussalam": "Brunei", "Republic of Cabo Verde": "Cabo Verde",
         "Czech Republic": "Czechia"}  # the names people use
OUT = Path(__file__).resolve().parents[1] / "src" / "worldwatch" / "config" / "places.json.gz"


def fetch(name: str) -> list[dict]:
    with urllib.request.urlopen(BASE + name + ".geojson", timeout=120) as r:
        return json.load(r)["features"]


def rings(geom: dict) -> list[list[list[float]]]:
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    out = []
    for poly in polys:
        for ring in poly:
            pts: list[list[float]] = []
            for lon, lat in ring:
                p = [round(lon, 2), round(lat, 2)]
                if not pts or max(abs(p[0] - pts[-1][0]), abs(p[1] - pts[-1][1])) >= STEP:
                    pts.append(p)
            if len(pts) >= 4:
                out.append([[round(x * 100), round(y * 100)] for x, y in pts])
    return out


def main() -> None:
    countries = []
    for f in fetch("ne_50m_admin_0_countries"):
        p = f["properties"]
        cc = p.get("ISO_A2_EH") if p.get("ISO_A2_EH") not in (None, "-99") else ""
        name = p.get("NAME_LONG") or p["NAME"]
        countries.append({"name": PLAIN.get(name, name), "cc": cc, "rings": rings(f["geometry"])})
    seas = []
    for f in fetch("ne_10m_geography_marine_polys"):
        p = f["properties"]
        name = p.get("name_en") or p.get("name")
        if not name or p.get("featurecla") in ("river", "reef", "lagoon"):
            continue
        name = name.title() if name.isupper() else name
        seas.append({"name": name, "kind": p.get("featurecla"), "rank": p.get("scalerank"),
                     "rings": rings(f["geometry"])})
    OUT.write_bytes(gzip.compress(json.dumps({
        "source": "Natural Earth (public domain): ne_50m_admin_0_countries, ne_10m_geography_marine_polys",
        "countries": countries, "seas": seas}, separators=(",", ":"), ensure_ascii=False).encode(), mtime=0))
    print(f"{OUT}: {len(countries)} countries, {len(seas)} seas, {OUT.stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
