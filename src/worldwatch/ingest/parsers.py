"""Parsers: raw HTTP response payload → list[Observation].

A parser is selected by the `format` key in a source's [parse] table and
registered in PARSERS.  Adding a source with an existing format needs no code
(guardrail 2); a genuinely new payload shape adds one function here.

All parsers field-drop at the door (guardrail 8): keep stream_id, cell, ts,
value, and a minimal meta dict; discard everything else.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from worldwatch.config.loader import SourceConfig
from worldwatch.ingest.geocode import fixed_cell, h3_cell
from worldwatch.ingest.models import Observation

Parser = Callable[[Any, SourceConfig], list[Observation]]

PARSERS: dict[str, Parser] = {}


def register(fmt: str) -> Callable[[Parser], Parser]:
    def deco(fn: Parser) -> Parser:
        PARSERS[fmt] = fn
        return fn

    return deco


def parse(payload: Any, cfg: SourceConfig) -> list[Observation]:
    fmt = cfg.parse.get("format")
    if fmt not in PARSERS:
        raise ValueError(f"No parser registered for format {fmt!r} (source {cfg.stream_id})")
    return PARSERS[str(fmt)](payload, cfg)


@register("geojson_features")
def parse_geojson_features(payload: Any, cfg: SourceConfig) -> list[Observation]:
    """GeoJSON FeatureCollection (e.g. USGS earthquakes).

    Each feature → one event Observation. Coordinates are [lon, lat, depth];
    time is epoch milliseconds. value_field/time_field/filter_min_mag come
    from the [parse] table.
    """
    value_field = str(cfg.parse.get("value_field", "mag"))
    time_field = str(cfg.parse.get("time_field", "time"))
    min_val = cfg.parse.get("filter_min_mag")
    resolution = int(cfg.geocode.get("h3_resolution", 3))

    obs: list[Observation] = []
    for feat in payload.get("features", []):
        props = feat.get("properties") or {}
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates")
        if not coords or len(coords) < 2:
            continue
        lon, lat = float(coords[0]), float(coords[1])

        raw_time = props.get(time_field)
        if raw_time is None:
            continue
        ts = int(raw_time) // 1000  # epoch ms → s

        val = props.get(value_field)
        val = float(val) if val is not None else None
        if min_val is not None and val is not None and val < float(min_val):
            continue

        cell = h3_cell(lat, lon, resolution)
        obs.append(
            Observation(
                stream_id=cfg.stream_id,
                cell=cell,
                ts=ts,
                value=val,
                meta={"depth_km": coords[2]} if len(coords) > 2 else None,
            )
        )
    return obs


@register("coinbase_spot")
def parse_coinbase_spot(payload: Any, cfg: SourceConfig) -> list[Observation]:
    """Single scalar price from a dotted value_path (e.g. 'data.amount')."""
    path = str(cfg.parse.get("value_path", "data.amount")).split(".")
    node: Any = payload
    for key in path:
        node = node[key]
    value = float(node)
    cell = fixed_cell(cfg.geocode)
    # No timestamp in the spot payload; caller stamps `now` via poll time.
    return [Observation(stream_id=cfg.stream_id, cell=cell, ts=_NOW_SENTINEL, value=value)]


@register("safecast_json")
def parse_safecast(payload: Any, cfg: SourceConfig) -> list[Observation]:
    """Safecast measurements: list of {value, latitude, longitude, captured_at}.

    captured_at is ISO-8601 UTC; rows without coords or time are dropped.
    """
    value_field = str(cfg.parse.get("value_field", "value"))
    resolution = int(cfg.geocode.get("h3_resolution", 4))
    records = payload if isinstance(payload, list) else payload.get("measurements", [])

    obs: list[Observation] = []
    for rec in records:
        lat, lon = rec.get("latitude"), rec.get("longitude")
        captured = rec.get("captured_at")
        val = rec.get(value_field)
        if lat is None or lon is None or captured is None or val is None:
            continue
        obs.append(
            Observation(
                stream_id=cfg.stream_id,
                cell=h3_cell(float(lat), float(lon), resolution),
                ts=_iso_to_epoch(str(captured)),
                value=float(val),
                meta={"unit": cfg.parse.get("unit")} if cfg.parse.get("unit") else None,
            )
        )
    return obs


@register("wikimedia_pageviews")
def parse_wikimedia_pageviews(payload: Any, cfg: SourceConfig) -> list[Observation]:
    """Wikimedia pageviews aggregate: items[] with `timestamp` (YYYYMMDDHH) and `views`."""
    cell = fixed_cell(cfg.geocode, default=str(cfg.parse.get("project", "wikipedia")))
    obs: list[Observation] = []
    for item in payload.get("items", []):
        stamp = str(item["timestamp"])  # e.g. 2026070912 (hour granularity)
        ts = _wiki_stamp_to_epoch(stamp)
        obs.append(
            Observation(
                stream_id=cfg.stream_id,
                cell=cell,
                ts=ts,
                value=float(item["views"]),
            )
        )
    return obs


# Sentinel: parser could not derive a timestamp; the poller substitutes poll time.
_NOW_SENTINEL = -1


def _wiki_stamp_to_epoch(stamp: str) -> int:
    """YYYYMMDDHH (UTC) → epoch seconds."""
    import calendar

    year = int(stamp[0:4])
    month = int(stamp[4:6])
    day = int(stamp[6:8])
    hour = int(stamp[8:10]) if len(stamp) >= 10 else 0
    return calendar.timegm((year, month, day, hour, 0, 0, 0, 0, 0))


def _iso_to_epoch(iso: str) -> int:
    """ISO-8601 (UTC, trailing Z tolerated) → epoch seconds."""
    from datetime import datetime

    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
