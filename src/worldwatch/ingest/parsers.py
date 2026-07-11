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
from worldwatch.ingest.geocode import fixed_cell, h3_cell, resolve_fixed
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


@register("geojson_events")
def parse_geojson_events(payload: Any, cfg: SourceConfig) -> list[Observation]:
    """General GeoJSON event feed (e.g. NWS/CAP severe-weather alerts).

    More permissive than geojson_features: geometry may be Point, Polygon, or
    MultiPolygon (reduced to a centroid) or null (skipped); time may be ISO-8601
    or epoch ms; value is optional (pure-event → count flavor). time_field falls
    back through a list so alert feeds with onset/effective/sent all work.
    """
    time_fields = cfg.parse.get("time_fields") or [
        cfg.parse.get("time_field", "onset"),
        "effective",
        "sent",
    ]
    value_field = cfg.parse.get("value_field")  # optional
    resolution = int(cfg.geocode.get("h3_resolution", 3))

    obs: list[Observation] = []
    for feat in payload.get("features", []):
        centroid = _feature_centroid(feat.get("geometry"))
        if centroid is None:
            continue
        lon, lat = centroid

        props = feat.get("properties") or {}
        raw_time = next((props[f] for f in time_fields if props.get(f) is not None), None)
        if raw_time is None:
            continue
        ts = _parse_event_time(raw_time)
        if ts is None:
            continue

        val = None
        if value_field is not None and props.get(value_field) is not None:
            val = float(props[value_field])

        obs.append(
            Observation(
                stream_id=cfg.stream_id,
                cell=h3_cell(lat, lon, resolution),
                ts=ts,
                value=val,
            )
        )
    return obs


def _feature_centroid(geometry: Any) -> tuple[float, float] | None:
    """(lon, lat) centroid of a GeoJSON geometry, or None if unusable.

    Zone-only alerts (null geometry) are skipped in P0 — resolving UGC/FIPS
    zones to coordinates needs a gazetteer, which is later work.
    """
    if not geometry:
        return None
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return None
    if gtype == "Point":
        return float(coords[0]), float(coords[1])
    if gtype == "Polygon":
        ring = coords[0]
    elif gtype == "MultiPolygon":
        ring = coords[0][0]
    else:
        return None
    lons = [float(p[0]) for p in ring]
    lats = [float(p[1]) for p in ring]
    return sum(lons) / len(lons), sum(lats) / len(lats)


def _parse_event_time(raw: Any) -> int | None:
    """Epoch seconds from an ISO-8601 string (offset or Z) or an epoch int.

    Integers >= 1e11 are treated as epoch milliseconds.
    """
    if isinstance(raw, int | float):
        v = int(raw)
        return v // 1000 if v >= 100_000_000_000 else v
    try:
        return _iso_to_epoch(str(raw))
    except ValueError:
        return None


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
    """Wikimedia pageviews aggregate: items[] with `timestamp` (YYYYMMDDHH) and
    `views`. One valued observation per hour (continuous flavor: the views ARE
    the signal, not the row count). transform = "log1p" stores log(1+views) —
    attention is multiplicative, and it keeps the fixed obs_scale workable
    until P1's online scale inference."""
    import math

    cell = fixed_cell(cfg.geocode, default=str(cfg.parse.get("project", "wikipedia")))
    use_log1p = str(cfg.parse.get("transform", "")) == "log1p"
    obs: list[Observation] = []
    for item in payload.get("items", []):
        stamp = str(item["timestamp"])  # e.g. 2026070912 (hour granularity)
        ts = _wiki_stamp_to_epoch(stamp)
        views = float(item["views"])
        obs.append(
            Observation(
                stream_id=cfg.stream_id,
                cell=cell,
                ts=ts,
                value=math.log1p(views) if use_log1p else views,
            )
        )
    return obs


@register("cloudflare_radar_timeseries")
def parse_cloudflare_radar_timeseries(payload: Any, cfg: SourceConfig) -> list[Observation]:
    """Cloudflare Radar timeseries: result.serie_0.{timestamps, values}.

    Values are min0_max-normalized over the queried dateRange (Radar exposes no
    raw values); the stanza uses a long window so the normalization anchor (the
    weekly traffic peak) is stable across polls. The final point is the
    in-progress bucket and is dropped; earlier points are final, so PK dedup
    across overlapping windows keeps consistent values.
    """
    serie = payload["result"]["serie_0"]
    cell = resolve_fixed(cfg.geocode)
    points = list(zip(serie["timestamps"], serie["values"], strict=True))[:-1]
    return [
        Observation(
            stream_id=cfg.stream_id,
            cell=cell,
            ts=_iso_to_epoch(str(stamp)),
            value=float(val),
        )
        for stamp, val in points
    ]


@register("vnp46a2_grid")
def parse_vnp46a2_grid(payload: Any, cfg: SourceConfig) -> list[Observation]:
    """NASA Black Marble VNP46A2 granule → per-H3-cell mean night radiance.

    payload comes from the earthdata_granule fetcher: {granule_id, time_start,
    content: HDF5 bytes}. Pixels are kept only where Mandatory_Quality_Flag == 0
    (high-quality main-algorithm retrieval) and the radiance is a real value;
    they are block-averaged, blocks are assigned to H3 cells, and each cell with
    enough valid coverage emits one observation at the granule's day. Cells
    below min_valid_frac are dropped, not imputed (guardrail 4): clouded or
    unretrievable regions go missing and the presence channel sees it.

    value = log1p(mean radiance in nW/(cm²·sr)) by default — night-lights
    radiance is heavy-tailed across cities vs countryside; the log keeps the
    continuous flavor's Student-t predictive sane on bright cells.
    """
    import io

    import h5py
    import numpy as np

    group_path = str(
        cfg.parse.get("grid_group", "HDFEOS/GRIDS/VIIRS_Grid_DNB_2d/Data Fields")
    )
    value_name = str(cfg.parse.get("value_dataset", "DNB_BRDF-Corrected_NTL"))
    quality_name = str(cfg.parse.get("quality_dataset", "Mandatory_Quality_Flag"))
    block = int(cfg.parse.get("block_pixels", 16))
    min_valid_frac = float(cfg.parse.get("min_valid_frac", 0.2))
    use_log1p = str(cfg.parse.get("transform", "log1p")) == "log1p"
    resolution = int(cfg.geocode.get("h3_resolution", 4))
    ts = _iso_to_epoch(str(payload["time_start"]))

    with h5py.File(io.BytesIO(payload["content"]), "r") as f:
        grid = f[group_path]
        ntl_ds = grid[value_name]
        fill = float(ntl_ds.attrs["_FillValue"][0]) if "_FillValue" in ntl_ds.attrs else -999.9
        ntl = ntl_ds[:].astype(np.float64)
        quality = grid[quality_name][:]
        lat = grid["lat"][:]
        lon = grid["lon"][:]

    # Trim to a whole number of blocks, then block-reduce valid-pixel sums.
    nrow = (ntl.shape[0] // block) * block
    ncol = (ntl.shape[1] // block) * block
    ntl, quality = ntl[:nrow, :ncol], quality[:nrow, :ncol]
    valid = (quality == 0) & (ntl != fill) & (ntl >= 0.0)

    shape4 = (nrow // block, block, ncol // block, block)
    value_sum = np.where(valid, ntl, 0.0).reshape(shape4).sum(axis=(1, 3))
    valid_count = valid.reshape(shape4).sum(axis=(1, 3))
    block_lat = lat[:nrow].reshape(nrow // block, block).mean(axis=1)
    block_lon = lon[:ncol].reshape(ncol // block, block).mean(axis=1)

    # Accumulate blocks into H3 cells: [valid-weighted sum, valid px, total px].
    cells: dict[str, list[float]] = {}
    for i in range(valid_count.shape[0]):
        for j in range(valid_count.shape[1]):
            cell = h3_cell(float(block_lat[i]), float(block_lon[j]), resolution)
            acc = cells.setdefault(cell, [0.0, 0.0, 0.0])
            acc[0] += value_sum[i, j]
            acc[1] += float(valid_count[i, j])
            acc[2] += block * block

    obs: list[Observation] = []
    for cell, (vsum, vcount, total) in sorted(cells.items()):
        if vcount == 0 or vcount / total < min_valid_frac:
            continue
        mean = vsum / vcount
        obs.append(
            Observation(
                stream_id=cfg.stream_id,
                cell=cell,
                ts=ts,
                value=float(np.log1p(mean)) if use_log1p else float(mean),
            )
        )
    return obs


@register("gdelt_export_events")
def parse_gdelt_export_events(payload: Any, cfg: SourceConfig) -> list[Observation]:
    """GDELT 2.0 export batch (zipped TSV, one row per event) → pure-event
    count observations for geocoded events (ActionGeo lat/lon; ungeocoded rows
    are dropped).

    Every row in a batch shares one DATEADDED stamp (the batch end), but
    raw_ring's PK is (stream, cell, ts), so same-cell events would collapse to
    one row. True per-event times are unknown below batch granularity; events
    are spread deterministically across the batch's window — per cell, ordered
    by event id — which is collision-free while a cell has ≤ window events.
    Same file → same rows, so re-ingest stays idempotent.

    Event *content* fields (actors, CAMEO codes, tone) are dropped at the door
    (guardrail 8): the P0 signal is geocoded news-event counts per cell.
    """
    import io
    import zipfile

    lat_col = int(cfg.parse.get("lat_col", 56))
    lon_col = int(cfg.parse.get("lon_col", 57))
    window = int(cfg.parse.get("batch_window_seconds", 900))
    resolution = int(cfg.geocode.get("h3_resolution", 3))
    batch_end = int(payload["batch_epoch"])

    with zipfile.ZipFile(io.BytesIO(payload["content"])) as z:
        text = z.read(z.namelist()[0]).decode("utf-8", errors="replace")

    by_cell: dict[str, list[int]] = {}
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) <= max(lat_col, lon_col):
            continue
        lat_s, lon_s = cols[lat_col].strip(), cols[lon_col].strip()
        if not lat_s or not lon_s:
            continue
        try:
            lat, lon, event_id = float(lat_s), float(lon_s), int(cols[0])
        except ValueError:
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        by_cell.setdefault(h3_cell(lat, lon, resolution), []).append(event_id)

    obs: list[Observation] = []
    for cell, event_ids in sorted(by_cell.items()):
        event_ids.sort()
        count = len(event_ids)
        for i in range(count):
            obs.append(
                Observation(
                    stream_id=cfg.stream_id,
                    cell=cell,
                    ts=batch_end - window + (i * window) // count,
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
