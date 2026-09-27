"""Generic parsers: most feeds are records with a time, a value and a place.

Two shapes cover most new sources with config alone (guardrail 2):

  records         one observation per record — JSON (a list at `records`, a
                  dotted path), CSV (fetch kind text_get) or several feeds at
                  once (multi_get) — with the time, value and place named in
                  the stanza
  cell_aggregate  one observation per place per poll: a count, median, mean or
                  fraction of the records in each H3 cell or country (aircraft,
                  ships, air sensors, probes). Identities and exact positions
                  never leave the parser (guardrail 8).

[parse] keys, both:
  records        dotted path to the list ("" = the payload; a dict is one record);
                 list-of-lists records take integer field names ("6")
  where          { field = [allowed values] };  where_min = { field = n }
  transform      "log" | "log1p"
[parse] keys, records:
  time_field     or time_fields = [...] joined with a space; time_format:
                 "iso" (default), "epoch", "epoch_ms", or a strptime format;
                 time_tz ("+04:00") for naive local times
  value_field    dotted (indices allowed: "timeseries.0.currentMeasurement.value");
                 or value_sum = [...]; or value_ratio = [[numerators], denominator]
                 with min_denominator; none: a pure event (count flavor);
                 min_value / max_value drop records outside (also in cell_aggregate)
  skip_last      drop the newest time in the payload (an in-progress bucket)
  id_fields      spread records sharing a time: + a stable offset < id_spread s
[parse] keys, cell_aggregate:
  aggregate      "count" (default) | "median" | "mean" | "fraction"
  value_field    what median/mean/fraction read; fraction_below = n
  min_records    places with fewer are skipped (default 1)
  time_field     payload-level time (dotted); else the newest record time
                 (record_time_field) or the poll time; floored to bucket_seconds
[geocode] strategy:
  fixed (cell, or fixed_latlon with lat/lon)  |  coords (lat_field, lon_field; or polygon_field: a list
  of {lat, lon} or [lon, lat], its centroid)  |  country (country_field: ISO-2)
  |  points (key_field, points = { key = [lat, lon] })
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from worldwatch import evidence
from worldwatch.config.loader import SourceConfig
from worldwatch.ingest.geocode import h3_cell, resolve_fixed
from worldwatch.ingest.models import Observation
from worldwatch.ingest.registry import _NOW_SENTINEL, register


def get_path(obj: Any, path: str) -> Any:
    """obj["a"]["b"][0] for "a.b.0"; "vals[type=P2].value" picks the first list
    element whose `type` is "P2"; None where missing."""
    if path == "":
        return obj
    node = obj
    for key in path.split("."):
        sel = None
        if key.endswith("]") and "[" in key:
            key, _, cond = key[:-1].partition("[")
            field, _, want = cond.partition("=")
            sel = (field, want)
        if key:
            if isinstance(node, list):
                try:
                    node = node[int(key)]
                except (ValueError, IndexError):
                    return None
            elif isinstance(node, dict):
                node = node.get(key)
            else:
                return None
        if sel is not None:
            node = next((x for x in node or [] if isinstance(x, dict) and str(x.get(sel[0])) == sel[1]), None)
        if node is None:
            return None
    return node


def _payload_records(payload: Any, cfg: SourceConfig) -> list[tuple[Any, Any]]:
    """(record, its document) pairs: JSON, CSV text, or several feeds."""
    path = str(cfg.parse.get("records", ""))
    docs: list[Any]
    if isinstance(payload, list) and payload and isinstance(payload[0], dict) and "text" in payload[0] \
            and ("target" in payload[0] or "url" in payload[0]):
        docs = [_decode(p["text"], cfg) for p in payload if p.get("text")]  # multi_get / linked_get
    elif isinstance(payload, dict) and set(payload) == {"url", "text"}:
        docs = [_decode(payload["text"], cfg)]  # text_get
    else:
        docs = [payload]
    out = []
    for doc in docs:
        recs = get_path(doc, path)
        if isinstance(recs, dict):
            recs = [recs]
        out += [(r, doc) for r in (recs or [])]
    return out


def _decode(text: str, cfg: SourceConfig) -> Any:
    if str(cfg.parse.get("text_format", "json")) == "csv":
        return list(csv.DictReader(io.StringIO(text)))
    return json.loads(text)


def _keep(rec: Any, cfg: SourceConfig) -> bool:
    for k, allowed in (cfg.parse.get("where") or {}).items():
        if get_path(rec, str(k)) not in list(allowed):
            return False
    for k, lo in (cfg.parse.get("where_min") or {}).items():
        v = _num(get_path(rec, str(k)))
        if v is None or v < float(lo):
            return False
    return True


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _transform(v: float, cfg: SourceConfig) -> float:
    t = str(cfg.parse.get("transform", ""))
    if t == "log":
        return math.log(max(v, 1e-12))
    if t == "log1p":
        return math.log1p(max(v, 0.0))
    return v


def parse_time(raw: Any, fmt: str = "iso", tz: str | None = None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        if fmt == "epoch":
            return int(float(raw))
        if fmt == "epoch_ms":
            return int(float(raw)) // 1000
        if fmt == "iso":
            s = str(raw).strip().replace("Z", "+00:00")
            if " " in s and "T" not in s:
                s = s.replace(" ", "T", 1)
            t = datetime.fromisoformat(s)
        else:
            t = datetime.strptime(str(raw).strip(), fmt)
    except (ValueError, TypeError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=_tz(tz) if tz else timezone.utc)
    return int(t.timestamp())


def _tz(spec: str) -> timezone:
    sign = -1 if spec.startswith("-") else 1
    h, _, m = spec.lstrip("+-").partition(":")
    return timezone(sign * timedelta(hours=int(h), minutes=int(m or 0)))


def _cell(rec: Any, cfg: SourceConfig) -> str | None:
    g = cfg.geocode
    res = int(g.get("h3_resolution", 3))
    strategy = str(g.get("strategy", "fixed"))
    if strategy == "coords":
        if g.get("polygon_field"):
            c = _centroid(get_path(rec, str(g["polygon_field"])))
            return h3_cell(c[0], c[1], res) if c else None
        lat, lon = _num(get_path(rec, str(g.get("lat_field", "lat")))), _num(get_path(rec, str(g.get("lon_field", "lon"))))
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            return None
        return h3_cell(lat, lon, res)
    if strategy == "country":
        from worldwatch.config.countries import country_cell

        cc = get_path(rec, str(g.get("country_field", "cc")))
        return country_cell(str(cc), res) if cc else None
    if strategy == "points":
        p = (g.get("points") or {}).get(str(get_path(rec, str(g.get("key_field", "id")))))
        return h3_cell(float(p[0]), float(p[1]), res) if p else None
    return resolve_fixed(g)  # fixed: a cell name, or fixed_latlon


def _centroid(pts: Any) -> tuple[float, float] | None:
    """(lat, lon) mean of a list of {lat, lon} or [lon, lat] points."""
    if not isinstance(pts, list) or not pts:
        return None
    lats, lons = [], []
    for p in pts:
        if isinstance(p, dict):
            lat, lon = _num(p.get("lat")), _num(p.get("lon"))
        elif isinstance(p, list | tuple) and len(p) >= 2:
            lon, lat = _num(p[0]), _num(p[1])
        else:
            continue
        if lat is not None and lon is not None:
            lats.append(lat)
            lons.append(lon)
    return (sum(lats) / len(lats), sum(lons) / len(lons)) if lats else None


def _time(rec: Any, cfg: SourceConfig) -> int | None:
    fields = cfg.parse.get("time_fields") or [cfg.parse.get("time_field", "time")]
    parts = [get_path(rec, str(f)) for f in fields]
    if any(p is None for p in parts):
        return None
    raw = " ".join(str(p) for p in parts) if len(parts) > 1 else parts[0]
    return parse_time(raw, str(cfg.parse.get("time_format", "iso")), cfg.parse.get("time_tz"))


def _value(rec: Any, cfg: SourceConfig) -> float | None | bool:
    """The record's value; None for a pure event; False to skip the record."""
    p = cfg.parse
    if p.get("value_ratio"):
        nums, den = p["value_ratio"]
        d = _num(get_path(rec, str(den)))
        if d is None or d < float(p.get("min_denominator", 1)) or d <= 0:
            return False
        n = sum(_num(get_path(rec, str(f))) or 0.0 for f in nums)
        return n / d
    if p.get("value_sum"):
        vals = [_num(get_path(rec, str(f))) for f in p["value_sum"]]
        return sum(v for v in vals if v is not None) if any(v is not None for v in vals) else False
    if p.get("value_field"):
        v = _num(get_path(rec, str(p["value_field"])))
        return False if v is None else v
    return None


def _in_range(v: float, cfg: SourceConfig) -> bool:
    lo, hi = cfg.parse.get("min_value"), cfg.parse.get("max_value")
    return (lo is None or v >= float(lo)) and (hi is None or v <= float(hi))


def _offset(rec: Any, cfg: SourceConfig) -> int:
    ids = cfg.parse.get("id_fields")
    if not ids:
        return 0
    key = "|".join(str(get_path(rec, str(f))) for f in ids)
    return int(hashlib.sha1(key.encode()).hexdigest()[:8], 16) % int(cfg.parse.get("id_spread", 600))


@register("records")
def parse_records(payload: Any, cfg: SourceConfig) -> list[Observation]:
    rows = []
    for rec, _ in _payload_records(payload, cfg):
        if not _keep(rec, cfg):
            continue
        ts, cell = _time(rec, cfg), _cell(rec, cfg)
        if ts is None or cell is None:
            continue
        v = _value(rec, cfg)
        if v is False or (v is not None and not _in_range(v, cfg)):
            continue
        rows.append((rec, cell, ts, None if v is None else _transform(float(v), cfg)))
    if cfg.parse.get("skip_last") and rows:
        newest = max(ts for _, _, ts, _ in rows)
        rows = [r for r in rows if r[2] < newest]
    site = cfg.parse.get("site_field")
    if site is not None:  # co-located sensors: one series per cell, the lowest id every poll
        low: dict[str, str] = {}
        for rec, cell, _, _ in rows:
            sid = str(get_path(rec, str(site)))
            low[cell] = min(low.get(cell, sid), sid)
        rows = [r for r in rows if str(get_path(r[0], str(site))) == low[r[1]]]
    return [Observation(cfg.stream_id, cell, ts + _offset(rec, cfg), v,
                        context=evidence.with_rank(cfg, evidence.pick(cfg, _flat(rec))))
            for rec, cell, ts, v in rows]


def _flat(rec: Any, prefix: str = "") -> dict[str, Any]:
    """A record as a flat dict for the evidence store: nested keys dotted
    ("water.shortname"), list records by index."""
    items = rec.items() if isinstance(rec, dict) else enumerate(rec) if isinstance(rec, list) else []
    out: dict[str, Any] = {}
    for k, v in items:
        key = f"{prefix}{k}"
        if isinstance(v, dict | list) and prefix.count(".") < 3:
            out.update(_flat(v, key + "."))
        else:
            out[key] = v
    return out


@register("cell_aggregate")
def parse_cell_aggregate(payload: Any, cfg: SourceConfig) -> list[Observation]:
    p = cfg.parse
    how = str(p.get("aggregate", "count"))
    vf = p.get("value_field")
    groups: dict[str, list[float]] = {}
    times: list[int] = []
    doc_time = None
    for rec, doc in _payload_records(payload, cfg):
        if doc_time is None and p.get("time_field"):
            doc_time = parse_time(get_path(doc, str(p["time_field"])), str(p.get("time_format", "iso")))
        if not _keep(rec, cfg):
            continue
        cell = _cell(rec, cfg)
        if cell is None:
            continue
        if p.get("record_time_field"):
            t = parse_time(get_path(rec, str(p["record_time_field"])), str(p.get("time_format", "iso")))
            if t is not None:
                times.append(t)
        if how == "count":
            groups.setdefault(cell, []).append(1.0)
            continue
        v = _num(get_path(rec, str(vf))) if vf else None
        if v is not None and _in_range(v, cfg):
            groups.setdefault(cell, []).append(v)
    ts = doc_time if doc_time is not None else (max(times) if times else _NOW_SENTINEL)
    bucket = int(p.get("bucket_seconds", 0))
    if bucket and ts != _NOW_SENTINEL:
        ts -= ts % bucket
    need = int(p.get("min_records", 1))
    obs = []
    for cell, vals in sorted(groups.items()):
        if len(vals) < need:
            continue
        if how == "count":
            v = float(len(vals))
        elif how == "median":
            v = float(statistics.median(vals))
        elif how == "mean":
            v = sum(vals) / len(vals)
        else:  # fraction
            v = sum(x < float(p.get("fraction_below", 0)) for x in vals) / len(vals)
        obs.append(Observation(cfg.stream_id, cell, ts, _transform(v, cfg)))
    return obs

