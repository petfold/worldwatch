"""Parsers: raw HTTP response payload → list[Observation].

A parser is selected by the `format` key in a source's [parse] table and
registered in PARSERS.  Adding a source with an existing format needs no code
(guardrail 2); a genuinely new payload shape adds one function here.

All parsers field-drop at the door (guardrail 8): keep stream_id, cell, ts,
value, a minimal meta dict, and — where the stanza names [context] fields —
the slim human-readable record for the evidence store; discard everything else.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from worldwatch import evidence
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
                context=evidence.with_rank(
                    cfg, evidence.pick(cfg, {**props, "depth_km": coords[2] if len(coords) > 2 else None})
                ),
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
    `where` keeps only features whose property takes one of the listed values
    (e.g. stations in operation); transform = "log" stores log(value);
    `site_field` names the sensor id, keeping one sensor per cell.
    """
    time_fields = cfg.parse.get("time_fields") or [
        cfg.parse.get("time_field", "onset"),
        "effective",
        "sent",
    ]
    value_field = cfg.parse.get("value_field")  # optional
    transform = str(cfg.parse.get("transform", ""))
    # where = { site_status = [1] }: keep features whose property is one of the values
    where = {str(k): list(v) for k, v in (cfg.parse.get("where") or {}).items()}
    site_field = cfg.parse.get("site_field")  # sensor networks: one site per cell
    # id_field: distinct items often share an onset (one warning, many counties);
    # a stable offset from the item's id (< 10 min) keeps them distinct keys
    id_field = cfg.parse.get("id_field")
    resolution = int(cfg.geocode.get("h3_resolution", 3))

    obs: list[Observation] = []
    for feat in payload.get("features", []):
        props = feat.get("properties") or {}
        if any(props.get(k) not in allowed for k, allowed in where.items()):
            continue
        centroid = _feature_centroid(feat.get("geometry"))
        if centroid is None:
            continue
        lon, lat = centroid

        raw_time = next((props[f] for f in time_fields if props.get(f) is not None), None)
        if raw_time is None:
            continue
        ts = _parse_event_time(raw_time)
        if ts is None:
            continue

        val = None
        if value_field is not None and props.get(value_field) is not None:
            val = float(props[value_field])
            if transform == "log":  # multiplicative quantities (dose rates)
                import math

                val = math.log(max(val, 1e-9))

        if id_field and props.get(id_field):
            import hashlib

            ts += int(hashlib.sha1(str(props[id_field]).encode()).hexdigest()[:6], 16) % 600
        obs.append(
            Observation(
                stream_id=cfg.stream_id,
                cell=h3_cell(lat, lon, resolution),
                ts=ts,
                value=val,
                context=evidence.with_rank(cfg, evidence.pick(cfg, props)),
                meta={"site": str(props.get(site_field))} if site_field else None,
            )
        )
    if site_field:
        obs = _one_site_per_cell(obs)
    return obs


def _one_site_per_cell(obs: list[Observation]) -> list[Observation]:
    """Co-located sensors (same fine cell) are one series, not several: keep
    the lowest site id in each cell, so the same detector is chosen every poll
    and two detectors are never mixed into one model. They are not independent
    confirmations either."""
    keep: dict[str, str] = {}
    for o in obs:
        site = str((o.meta or {}).get("site"))
        if o.cell not in keep or site < keep[o.cell]:
            keep[o.cell] = site
    return [o for o in obs if str((o.meta or {}).get("site")) == keep[o.cell]]


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
    """Single scalar price from a dotted value_path (e.g. 'data.amount').

    transform = "log1p" stores log(1+price): prices move multiplicatively, and
    the log keeps the fixed obs_scale workable until P1's online scale
    inference (raw ~1e4-scale prices under the default obs_scale pin the PIT).
    """
    import math

    path = str(cfg.parse.get("value_path", "data.amount")).split(".")
    node: Any = payload
    for key in path:
        node = node[key]
    value = float(node)
    if str(cfg.parse.get("transform", "")) == "log1p":
        value = math.log1p(value)
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
    """GDELT 2.0 export batch (zipped TSV, one row per coded event) → one
    pure-event observation per distinct article per cell (ActionGeo lat/lon;
    ungeocoded rows are dropped).

    Every row in a batch shares one DATEADDED stamp (the batch end), but
    raw_ring's PK is (stream, cell, ts), so same-cell events would collapse to
    one row. True per-event times are unknown below batch granularity; events
    are spread deterministically across the batch's window — per cell, ordered
    by event id — which is collision-free while a cell has ≤ window events.
    Same file → same rows, so re-ingest stays idempotent.

    The detection signal is geocoded news-event counts per cell. With a
    [context] table, each event also keeps its named columns (actors, place,
    mentions, tone, article URL…) plus `action`, the CAMEO root code in words,
    for the evidence store; the rest of the row is dropped at the door.
    """
    import io
    import zipfile

    lat_col = int(cfg.parse.get("lat_col", 56))
    lon_col = int(cfg.parse.get("lon_col", 57))
    window = int(cfg.parse.get("batch_window_seconds", 900))
    url_col = int(cfg.parse.get("url_col", 60))
    resolution = int(cfg.geocode.get("h3_resolution", 3))
    batch_end = int(payload["batch_epoch"])

    with zipfile.ZipFile(io.BytesIO(payload["content"])) as z:
        text = z.read(z.namelist()[0]).decode("utf-8", errors="replace")

    ctx_cols: dict[str, int] = {
        str(k): int(v) for k, v in ((evidence.spec(cfg) or {}).get("columns") or {}).items()
    }
    by_cell: dict[str, list[tuple[int, dict[str, object] | None]]] = {}
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
        url = cols[url_col].strip() if url_col < len(cols) else ""
        ctx = None
        if ctx_cols:
            raw = {name: cols[i].strip() for name, i in ctx_cols.items() if i < len(cols)}
            for name in ("mentions", "sources", "articles"):
                if raw.get(name, "").isdigit():
                    raw[name] = int(raw[name])  # type: ignore[assignment]
            if raw.get("tone"):
                try:
                    raw["tone"] = round(float(raw["tone"]), 1)  # type: ignore[assignment]
                except ValueError:
                    pass
            raw["action"] = CAMEO_ROOTS.get(str(raw.get("root", "")), "")
            raw["headline"] = headline_from_url(str(raw.get("url", "")))
            raw["actors"] = " / ".join(
                a.title() for a in (raw.get("actor1"), raw.get("actor2")) if isinstance(a, str) and a
            )
            ctx = evidence.with_rank(cfg, evidence.pick(cfg, raw))
        by_cell.setdefault(h3_cell(lat, lon, resolution), []).append((event_id, url, ctx))

    obs: list[Observation] = []
    for cell, coded in sorted(by_cell.items()):
        # GDELT codes one article as ~3 events; the independent unit is the
        # article (distinct SOURCEURL), which brings a cell's counts close to
        # Poisson. Keep the most-mentioned coding of each article as its context.
        best: dict[str, tuple[int, dict[str, object] | None]] = {}
        for event_id, url, ctx in sorted(coded, key=lambda e: e[0]):
            key = url or str(event_id)
            rank = int((ctx or {}).get("_rank") or 0)
            if key not in best or rank > int((best[key][1] or {}).get("_rank") or 0):
                best[key] = (event_id, ctx)
        events = sorted(best.values(), key=lambda e: e[0])
        count = len(events)
        for i, (_, ctx) in enumerate(events):
            obs.append(
                Observation(
                    stream_id=cfg.stream_id,
                    cell=cell,
                    ts=batch_end - window + (i * window) // count,
                    context=ctx,
                )
            )
    return obs


def headline_from_url(url: str) -> str:
    """A readable pseudo-headline from a news URL's slug — GDELT carries no
    title, but most sites put one in the path ('…/jay-slaters-mum-glanced-37704758'
    → 'Jay slaters mum glanced'). Empty when the path has no wordy segment."""
    import re
    from urllib.parse import unquote, urlparse

    segments = [s for s in unquote(urlparse(url).path).split("/") if s]
    best = ""
    for seg in segments:
        seg = re.sub(r"\.(s?html?|php|aspx?)$", "", seg, flags=re.I)
        seg = re.sub(r"^\d+[._-]", "", seg)  # '26555931.south-wales-…' ids
        words = [w for w in re.split(r"[-_+\s]+", seg) if w and not re.fullmatch(r"[\d.]+|[0-9a-f]{8,}", w, re.I)]
        if len(words) >= 3 and len(" ".join(words)) > len(best):
            best = " ".join(words)
    return (best[:1].upper() + best[1:])[:160] if best else ""


# CAMEO event root codes (GDELT EventRootCode) in plain words.
CAMEO_ROOTS: dict[str, str] = {
    "01": "Public statement", "02": "Appeal", "03": "Intent to cooperate",
    "04": "Consultation", "05": "Diplomatic cooperation", "06": "Material cooperation",
    "07": "Aid", "08": "Concession", "09": "Investigation", "10": "Demand",
    "11": "Disapproval", "12": "Rejection", "13": "Threat", "14": "Protest",
    "15": "Show of force", "16": "Reduced relations", "17": "Coercion",
    "18": "Assault", "19": "Fighting", "20": "Mass violence",
}


@register("coinbase_ticker")
def parse_coinbase_ticker(payload: Any, cfg: SourceConfig) -> list[Observation]:
    """One Coinbase WebSocket `ticker` message → one price observation.

    payload = {"message": <decoded JSON>, "received": epoch}. Messages for
    other products or of other types (subscriptions, heartbeats) yield nothing;
    the stream runner's throttle thins the ~8 ticks/s to what the model needs.
    transform = "log1p" as for the REST spot price.
    """
    import math

    m = payload.get("message") or {}
    if m.get("type") != "ticker" or m.get("product_id") != cfg.parse.get("product_id"):
        return []
    price = float(m["price"])
    value = math.log1p(price) if str(cfg.parse.get("transform", "")) == "log1p" else price
    ts = _parse_event_time(m["time"]) if m.get("time") else _NOW_SENTINEL
    return [Observation(cfg.stream_id, str(cfg.geocode.get("cell", "GLOBAL")), ts, value)]


@register("emsc_ws")
def parse_emsc_ws(payload: Any, cfg: SourceConfig) -> list[Observation]:
    """One EMSC seismicportal WebSocket message ({"action": create|update,
    "data": GeoJSON feature}) → one quake observation (value = magnitude).

    Updates to already-seen events dedup on (cell, origin time); a revised
    location or origin time counts as a new key, which is rare and accepted.
    Context adds the EMSC event page as `url`.
    """
    m = payload.get("message") or {}
    feat = m.get("data") or {}
    props = feat.get("properties") or {}
    if props.get("lat") is None or props.get("lon") is None or not props.get("time"):
        return []
    ts = _parse_event_time(props["time"])
    if ts is None:
        return []
    mag = props.get("mag")
    min_mag = cfg.parse.get("filter_min_mag")
    if min_mag is not None and mag is not None and float(mag) < float(min_mag):
        return []
    unid = props.get("unid") or feat.get("id")
    source = {**props, "url": f"https://www.seismicportal.eu/eventdetails.html?unid={unid}" if unid else None}
    return [
        Observation(
            stream_id=cfg.stream_id,
            cell=h3_cell(float(props["lat"]), float(props["lon"]), int(cfg.geocode.get("h3_resolution", 3))),
            ts=ts,
            value=float(mag) if mag is not None else None,
            meta={"depth_km": props.get("depth")} if props.get("depth") is not None else None,
            context=evidence.with_rank(cfg, evidence.pick(cfg, source)),
        )
    ]


IODA_DASHBOARD = "https://ioda.inetintel.cc.gatech.edu/country/{cc}?from={start}&until={end}"


@register("ioda_events")
def parse_ioda_events(payload: Any, cfg: SourceConfig) -> list[Observation]:
    """IODA outage events (`/v2/outages/events?entityType=country&format=codf`)
    → one observation per (country, datasource, start); value = score.

    Long-running events are re-listed every poll with the same start and
    dedup on (cell, ts); datasources get distinct ts (start + index) so two
    signals starting together are both kept. Context links the IODA
    dashboard for the event's window.
    """
    from worldwatch.config.countries import country_cell, country_name

    res = int(cfg.geocode.get("h3_resolution", 2))
    min_score = cfg.parse.get("filter_min_score")
    ds_order = {d: i for i, d in enumerate(("bgp", "ping-slash24", "merit-nt", "gtr"))}
    obs: list[Observation] = []
    for e in (payload or {}).get("data") or []:
        loc = str(e.get("location") or "")
        if not loc.startswith("country/"):
            continue
        cc = loc.split("/", 1)[1]
        cell = country_cell(cc, res)
        if cell is None or e.get("start") is None:
            continue
        score = e.get("score")
        if min_score is not None and (score is None or float(score) < float(min_score)):
            continue
        start, duration = int(e["start"]), int(e.get("duration") or 0)
        ds = str(e.get("datasource") or "")
        source = {
            "country": e.get("location_name") or country_name(cc), "cc": cc, "datasource": ds,
            "score": round(float(score), 1) if score is not None else None,
            "duration_h": round(duration / 3600, 1),
            "url": IODA_DASHBOARD.format(cc=cc, start=start - 3600, end=start + max(duration, 3600)),
        }
        obs.append(Observation(
            cfg.stream_id, cell, start + ds_order.get(ds, 9),
            float(score) if score is not None else None,
            context=evidence.with_rank(cfg, evidence.pick(cfg, source)),
        ))
    return obs


@register("ioda_alerts")
def parse_ioda_alerts(payload: Any, cfg: SourceConfig) -> list[Observation]:
    """IODA per-datasource alerts (`/v2/outages/alerts?entityType=country`) →
    one pure-event observation per (country, datasource, time): a count stream
    of how many of IODA's signals dropped below their history in a country."""
    from worldwatch.config.countries import country_cell

    res = int(cfg.geocode.get("h3_resolution", 2))
    ds_order = {d: i for i, d in enumerate(("bgp", "ping-slash24", "merit-nt", "gtr"))}
    obs: list[Observation] = []
    for a in (payload or {}).get("data") or []:
        ent = a.get("entity") or {}
        if ent.get("type") != "country" or a.get("time") is None:
            continue
        cell = country_cell(str(ent.get("code") or ""), res)
        if cell is None:
            continue
        ds = str(a.get("datasource") or "")
        value, hist = a.get("value"), a.get("historyValue")
        source = {
            "country": ent.get("name"), "cc": ent.get("code"), "datasource": ds,
            "level": a.get("level"),
            "ratio": round(float(value) / float(hist), 3) if value is not None and hist else None,
        }
        obs.append(Observation(
            cfg.stream_id, cell, int(a["time"]) + ds_order.get(ds, 9), None,
            context=evidence.with_rank(cfg, evidence.pick(cfg, source)),
        ))
    return obs


_CAP_LEVEL = {"Moderate": "yellow", "Severe": "orange", "Extreme": "red"}


@register("meteoalarm_atom")
def parse_meteoalarm_atom(payload: Any, cfg: SourceConfig) -> list[Observation]:
    """MeteoAlarm legacy Atom feeds (one per country, via multi_get) → one
    pure-event observation per warning of the stanza's `severities`, in the
    country's cell (warnings carry region codes, not coordinates).

    Each warning's ts is its onset plus a stable offset from its CAP identifier
    (< 10 min), so warnings with the same onset stay distinct and re-polls
    dedup. Context: event type, awareness colour, area, and the MeteoAlarm page.
    """
    import hashlib
    import xml.etree.ElementTree as ET

    from worldwatch.config.countries import country_cell, country_name

    ns = {"a": "http://www.w3.org/2005/Atom", "cap": "urn:oasis:names:tc:emergency:cap:1.2"}
    keep = set(cfg.parse.get("severities") or ["Severe", "Extreme"])
    res = int(cfg.geocode.get("h3_resolution", 3))
    obs: list[Observation] = []
    for feed in payload or []:
        if not feed.get("text"):
            continue
        cell = country_cell(str(feed.get("cc", "")), res)
        if cell is None:
            continue
        try:
            root = ET.fromstring(feed["text"])
        except ET.ParseError:
            continue
        for e in root.findall("a:entry", ns):
            sev = (e.findtext("cap:severity", default="", namespaces=ns) or "").strip()
            if sev not in keep:
                continue
            onset = e.findtext("cap:onset", default="", namespaces=ns) or e.findtext("cap:sent", default="", namespaces=ns)
            ts = _parse_event_time(onset) if onset else None
            ident = e.findtext("cap:identifier", default="", namespaces=ns) or e.findtext("a:id", default="", namespaces=ns)
            if ts is None or not ident:
                continue
            offset = int(hashlib.sha1(ident.encode()).hexdigest()[:6], 16) % 600
            link = next((lk.get("href") for lk in e.findall("a:link", ns)
                         if lk.get("href", "").startswith("https://meteoalarm.org")), None)
            source = {
                "country": country_name(str(feed["cc"])), "cc": feed["cc"],
                "title": (e.findtext("a:title", default="", namespaces=ns) or "").strip(),
                "event": (e.findtext("cap:event", default="", namespaces=ns) or "").strip(),
                "level": _CAP_LEVEL.get(sev, sev.lower()), "severity": sev,
                "area": (e.findtext("cap:areaDesc", default="", namespaces=ns) or "").strip(),
                "url": link,
            }
            obs.append(Observation(cfg.stream_id, cell, ts + offset, None,
                                   context=evidence.with_rank(cfg, evidence.pick(cfg, source))))
    return obs


_GDACS_TYPES = {"EQ": "Earthquake", "TC": "Tropical cyclone", "FL": "Flood", "VO": "Volcano",
                "DR": "Drought", "WF": "Wildfire", "TS": "Tsunami"}


@register("gdacs_events")
def parse_gdacs_events(payload: Any, cfg: SourceConfig) -> list[Observation]:
    """GDACS event list (GeoJSON) → one observation per *current* event, keyed
    by its start time plus the alert level (Orange 0, Red 1), so an escalation
    from Orange to Red is a new observation. Context: type, name, country,
    level, and the GDACS report."""
    res = int(cfg.geocode.get("h3_resolution", 3))
    level_offset = {"Green": 0, "Orange": 1, "Red": 2}
    obs: list[Observation] = []
    for feat in (payload or {}).get("features") or []:
        p = feat.get("properties") or {}
        if str(p.get("iscurrent", "")).lower() != "true":
            continue
        c = _feature_centroid(feat.get("geometry"))
        ts = _parse_event_time(p["fromdate"] + "Z") if p.get("fromdate") else None
        if c is None or ts is None:
            continue
        kind, eid = str(p.get("eventtype", "")), p.get("eventid")
        source = {
            "type": _GDACS_TYPES.get(kind, kind), "name": p.get("name"), "country": p.get("country"),
            "level": p.get("alertlevel"), "episode": p.get("episodeid"),
            "url": f"https://www.gdacs.org/report.aspx?eventid={eid}&eventtype={kind}" if eid else None,
        }
        obs.append(Observation(cfg.stream_id, h3_cell(c[1], c[0], res),
                               ts + level_offset.get(str(p.get("alertlevel")), 0), None,
                               context=evidence.with_rank(cfg, evidence.pick(cfg, source))))
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
