"""Country points: ISO 3166-1 alpha-2 → a representative point inside the country.

For sources keyed by country (IODA, the prober's per-country aggregates), the
country's cell is the H3 cell of its Natural Earth label point — a point
guaranteed inside the country, unlike a centroid. Public-domain data, see
countries.csv.
"""

from __future__ import annotations

import csv
from functools import cache
from pathlib import Path

import h3

_CSV = Path(__file__).parent / "countries.csv"


@cache
def countries() -> dict[str, tuple[str, float, float]]:
    """cc → (name, lat, lon)."""
    with _CSV.open() as fh:
        rows = csv.DictReader(line for line in fh if not line.startswith("#"))
        return {r["cc"]: (r["name"], float(r["lat"]), float(r["lon"])) for r in rows}


def country_cell(cc: str, resolution: int) -> str | None:
    c = countries().get(cc.upper())
    return None if c is None else str(h3.latlng_to_cell(c[1], c[2], resolution))


def country_name(cc: str) -> str:
    c = countries().get(cc.upper())
    return c[0] if c else cc
