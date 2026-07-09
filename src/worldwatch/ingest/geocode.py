"""Assign a `cell` string to an observation per the source's geocode strategy.

Strategies (from the config stanza's [<source>.geocode] table):
  feature_coords  — H3 cell from per-feature lat/lon at h3_resolution
  global          — a single fixed cell (e.g. "GLOBAL"); non-spatial streams
  project_entity  — named-entity cell (e.g. a Wikipedia project); non-spatial
"""

from __future__ import annotations

import h3


def h3_cell(lat: float, lon: float, resolution: int) -> str:
    """H3 cell id (string) for a lat/lon at the given resolution."""
    return str(h3.latlng_to_cell(lat, lon, resolution))


def fixed_cell(geocode: dict[str, object], default: str = "GLOBAL") -> str:
    """Cell for non-spatial strategies (global / project_entity)."""
    return str(geocode.get("cell", default))
