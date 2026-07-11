"""Assign a `cell` string to an observation per the source's geocode strategy.

Strategies (from the config stanza's [<source>.geocode] table):
  feature_coords  — H3 cell from per-feature lat/lon at h3_resolution
  global          — a single fixed cell (e.g. "GLOBAL"); non-spatial streams
  project_entity  — named-entity cell (e.g. a Wikipedia project); non-spatial
  fixed_latlon    — one H3 cell from a stanza-given lat/lon (e.g. a country
                    centroid), so whole-region streams still join spatial
                    coherence grouping
"""

from __future__ import annotations

import h3


def h3_cell(lat: float, lon: float, resolution: int) -> str:
    """H3 cell id (string) for a lat/lon at the given resolution."""
    return str(h3.latlng_to_cell(lat, lon, resolution))


def fixed_cell(geocode: dict[str, object], default: str = "GLOBAL") -> str:
    """Cell for non-spatial strategies (global / project_entity)."""
    return str(geocode.get("cell", default))


def resolve_fixed(geocode: dict[str, object], default: str = "GLOBAL") -> str:
    """Cell for whole-stream strategies: fixed_latlon → an H3 cell, else the
    configured fixed cell string."""
    if geocode.get("strategy") == "fixed_latlon":
        resolution = int(str(geocode.get("h3_resolution", 2)))
        return h3_cell(float(str(geocode["lat"])), float(str(geocode["lon"])), resolution)
    return fixed_cell(geocode, default)


def coarsen(cell: str, resolution: int) -> str:
    """Map an H3 cell up to a coarser resolution for cross-source grouping.

    Sources geocode at different H3 resolutions; coarsening to a common parent
    lets nearby fine cells from different feeds land in the same region. Non-H3
    cells (GLOBAL, entity names, sentinels) pass through unchanged.
    """
    if not h3.is_valid_cell(cell):
        return cell
    if h3.get_resolution(cell) <= resolution:
        return cell
    return str(h3.cell_to_parent(cell, resolution))
