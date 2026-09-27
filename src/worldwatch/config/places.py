"""Place names: which country, sea or ocean a point is in (presentation only).

Pushes and alert reports say "Japan" or "Sea of Japan, off Japan", not only
"35.7N 139.7E". Outlines are Natural Earth's (public domain), thinned to ~10 km
(tools/build_places.py builds places.json.gz): good for naming, not for borders,
so a point in a coastal gap takes the nearest country or sea.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path

import h3
import numpy as np

_DATA = Path(__file__).parent / "places.json.gz"
OFF_COAST_KM = 150  # a sea point this close to a country is "off" it
COAST_GAP_KM = 30  # a point in no outline this close to a country is in it (thinned coasts)


@dataclass(frozen=True)
class Area:
    name: str
    rank: int  # seas: Natural Earth scalerank (0 an ocean; higher, smaller and more specific)
    rings: tuple[np.ndarray, ...]  # (n, 2) lon, lat
    bbox: tuple[float, float, float, float]  # west, south, east, north

    def contains(self, lon: float, lat: float) -> bool:
        w, s, e, n = self.bbox
        if not (w <= lon <= e and s <= lat <= n):
            return False
        inside = False
        for r in self.rings:  # even-odd over every ring: holes cancel
            x, y = r[:, 0], r[:, 1]
            x2, y2 = np.roll(x, -1), np.roll(y, -1)
            crosses = (y > lat) != (y2 > lat)
            with np.errstate(divide="ignore", invalid="ignore"):
                xs = x + (lat - y) * (x2 - x) / (y2 - y)
            inside ^= bool(np.count_nonzero(crosses & (lon < xs)) % 2)
        return inside

    def distance_km(self, lon: float, lat: float) -> float:
        """To the nearest outline point (points ~10 km apart: good enough here)."""
        pts = np.concatenate(self.rings)
        la1, la2 = np.radians(lat), np.radians(pts[:, 1])
        dlon = np.radians(pts[:, 0] - lon)
        a = np.sin((la2 - la1) / 2) ** 2 + np.cos(la1) * np.cos(la2) * np.sin(dlon / 2) ** 2
        return float(2 * 6371.0 * np.arcsin(np.sqrt(a.min())))


def _areas(features: list[dict]) -> list[Area]:
    out = []
    for f in features:
        rings = tuple(np.asarray(r, dtype=float) / 100.0 for r in f["rings"])
        if not rings:
            continue
        pts = np.concatenate(rings)
        out.append(Area(f["name"], int(f.get("rank") or 0), rings,
                        (pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max())))
    return out


@cache
def _load() -> tuple[list[Area], list[Area]]:
    d = json.loads(gzip.decompress(_DATA.read_bytes()))
    return _areas(d["countries"]), _areas(d["seas"])


def _nearest(areas: list[Area], lon: float, lat: float, within_km: float) -> tuple[str, float] | None:
    margin = within_km / 111.0 + 0.5
    best = None
    for a in areas:
        w, s, e, n = a.bbox
        if lat < s - margin or lat > n + margin or ((lon < w - margin * 2 or lon > e + margin * 2)
                                                     and e - w < 350):
            continue
        d = a.distance_km(lon, lat)
        if d <= within_km and (best is None or d < best[1]):
            best = (a.name, d)
    return best


@lru_cache(maxsize=4096)
def place_name(lat: float, lon: float) -> str:
    """'Japan', 'Sea of Japan, off Japan', 'North Pacific Ocean' ('' if unknown)."""
    lat, lon = round(lat, 2), round(lon, 2)
    countries, seas = _load()
    for c in countries:
        if c.contains(lon, lat):
            return c.name
    sea = max((s for s in seas if s.contains(lon, lat)), key=lambda s: s.rank, default=None)
    if sea is None:
        near = _nearest(countries, lon, lat, COAST_GAP_KM)
        if near:
            return near[0]
        near = _nearest(seas, lon, lat, OFF_COAST_KM)
        return near[0] if near else ""
    off = _nearest(countries, lon, lat, OFF_COAST_KM)
    return f"{sea.name}, off {off[0]}" if off else sea.name


def cell_place(cell: str) -> str:
    """The place name of an H3 cell's centre ('' for GLOBAL, entities, unknown)."""
    if not h3.is_valid_cell(cell):
        return ""
    return place_name(*h3.cell_to_latlng(cell))
