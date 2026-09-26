"""Read-only dashboard API + map (architecture §12).

Serves the surprise field as GeoJSON, per-(stream, cell) timelines, and the
alert feed, plus a single-page MapLibre view. The only write is alert feedback
labelling (for evaluation, §13).

`create_app(db_path)` opens a fresh connection per request from an
already-initialized database, so the API runs as its own process against the
same SQLite file the pipeline writes (WAL allows concurrent readers).
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import time
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

import h3
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from worldwatch import evidence
from worldwatch.api import context
from worldwatch.api.notify import format_alert
from worldwatch.cascade.bins import bin_width
from worldwatch.layer0.native import NATIVE_SCALE, row_seconds
from worldwatch.config.countries import countries
from worldwatch.config.loader import SourceConfig
from worldwatch.ingest.geocode import coarsen
from worldwatch.db import connect

_STATIC = Path(__file__).parent / "static"
DEFAULT_LOOKBACK_SECONDS = 24 * 3600
TAIL_EXTREMITY = 0.99  # matches the alert engine's default q tail
_OK_EVENTS = ("ok", "not_modified")
_ERROR_EVENTS = ("timeout", "http_error", "fetch_error", "parse_error")
_extremity = context.extremity
MAP_QUOTA = 5  # dots per stream per coarse region (coverage balance)
MAP_REGION_RESOLUTION = 2
THIN_COVERAGE = 3  # fewer own probe targets than this: "thin"


def _cell_polygon(cell: str) -> list[list[float]] | None:
    """GeoJSON [lng, lat] ring for an H3 cell, or None if not an H3 cell."""
    if not h3.is_valid_cell(cell):
        return None
    ring = [[lng, lat] for lat, lng in h3.cell_to_boundary(cell)]
    ring.append(ring[0])  # close the ring
    return ring


class Label(BaseModel):
    label: str  # true | false_positive | unclear


def create_app(
    db_path: Path,
    now_fn: object = time.time,
    sources: dict[str, SourceConfig] | None = None,
) -> FastAPI:
    """`sources` (the loaded stanzas) supply labels/units for the plain-language
    context; without them the API still works, showing raw stream ids."""
    app = FastAPI(title="Worldwatch", docs_url="/api/docs")
    cfgs: dict[str, SourceConfig] = sources or {}

    def get_conn() -> Iterator[sqlite3.Connection]:
        # One connection per request, but FastAPI may open and close it on
        # different threadpool threads — hence check_same_thread=False.
        conn = connect(db_path, check_same_thread=False)
        try:
            yield conn
        finally:
            conn.close()

    def _now() -> int:
        return int(now_fn())  # type: ignore[operator]

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_STATIC / "index.html")

    @app.get("/about")
    def about() -> FileResponse:
        """What Worldwatch is, what it actively measures, and how to opt out."""
        return FileResponse(_STATIC / "about.html")

    @app.get("/api/surprise.geojson")
    def surprise_geojson(
        lookback: int = DEFAULT_LOOKBACK_SECONDS,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> JSONResponse:
        cutoff = _now() - lookback
        rows = conn.execute(
            "SELECT stream_id, cell, q_value, presence_q FROM surprise "
            "WHERE bin_start >= ? AND q_value IS NOT NULL",
            (cutoff,),
        ).fetchall()
        # per geographic cell: max surprise + contributing streams
        polys: dict[str, list[list[float]]] = {}
        max_surprise: dict[str, float] = {}
        streams: dict[str, set[str]] = {}
        for r in rows:
            poly = _cell_polygon(r["cell"])
            if poly is None:
                continue
            cell = r["cell"]
            ext = _extremity(r["q_value"], r["presence_q"])
            polys[cell] = poly
            max_surprise[cell] = max(max_surprise.get(cell, 0.0), ext)
            streams.setdefault(cell, set()).add(r["stream_id"])
        features = [
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [poly]},
                "properties": {
                    "cell": cell,
                    "where": context.where(cell),
                    "surprise": round(max_surprise[cell], 4),
                    "n_streams": len(streams[cell]),
                    "streams": ", ".join(
                        context.display_for(sid, cfgs.get(sid)).label for sid in sorted(streams[cell])
                    ),
                },
            }
            for cell, poly in polys.items()
        ]
        return JSONResponse({"type": "FeatureCollection", "features": features})

    @app.get("/api/silence")
    def silence(
        lookback: int = DEFAULT_LOOKBACK_SECONDS,
        threshold: float = 0.9,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> JSONResponse:
        cutoff = _now() - lookback
        rows = conn.execute(
            "SELECT stream_id, MAX(presence_q) AS q, MAX(bin_start) AS last_bin "
            "FROM surprise WHERE bin_start >= ? AND q_value IS NULL AND presence_q >= ? "
            "GROUP BY stream_id ORDER BY q DESC",
            (cutoff, threshold),
        ).fetchall()
        return JSONResponse(
            {"silent_sources": [dict(r) for r in rows]}  # stream_id, q, last_bin
        )

    @app.get("/api/timeline")
    def timeline(
        stream: str,
        cell: str,
        scale: int = 0,
        limit: int = 500,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> JSONResponse:
        rows = conn.execute(
            "SELECT bin_start, q_value, presence_q, n_obs FROM surprise "
            "WHERE stream_id = ? AND cell = ? AND scale = ? ORDER BY bin_start DESC LIMIT ?",
            (stream, cell, scale, limit),
        ).fetchall()
        return JSONResponse(
            {
                "stream_id": stream,
                "cell": cell,
                "scale": scale,
                "points": [dict(r) for r in reversed(rows)],
            }
        )

    @app.get("/api/alerts")
    def alerts(
        limit: int = 100,
        status: str | None = None,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> JSONResponse:
        if status:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE status = ? ORDER BY opened_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM alerts ORDER BY opened_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return JSONResponse({"alerts": [_alert_dict(r, conn, cfgs) for r in rows]})

    @app.get("/api/alerts/{alert_id}")
    def alert_detail(alert_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> JSONResponse:
        row = conn.execute("SELECT * FROM alerts WHERE alert_id = ?", (alert_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="alert not found")
        return JSONResponse(_alert_dict(row, conn, cfgs))

    @app.post("/api/alerts/{alert_id}/label")
    def label_alert(
        alert_id: int, body: Label, conn: sqlite3.Connection = Depends(get_conn)
    ) -> JSONResponse:
        if body.label not in ("true", "false_positive", "unclear"):
            raise HTTPException(status_code=422, detail="invalid label")
        cur = conn.execute("UPDATE alerts SET label = ? WHERE alert_id = ?", (body.label, alert_id))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="alert not found")
        return JSONResponse({"alert_id": alert_id, "label": body.label})


    @app.get("/api/overview")
    def overview(
        lookback: int = DEFAULT_LOOKBACK_SECONDS,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> JSONResponse:
        """Per-source picture of the collected data, calm or not: health,
        latest value in natural units, a sparkline, and the day's most
        surprising bin. Context for a human, not a detection input."""
        now = _now()
        cutoff = now - lookback
        stream_ids = [sid for sid, c in cfgs.items() if c.status != "retired"] or [
            r[0] for r in conn.execute("SELECT stream_id FROM sources ORDER BY stream_id")
        ]
        health = _health_summary(conn, cutoff)
        bins: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in conn.execute(
            "SELECT stream_id, cell, scale, bin_start, n, vmin, vmax, vmean FROM bins "
            "WHERE bin_start >= ? ORDER BY bin_start",
            (cutoff,),
        ):
            bins[r["stream_id"]].append(r)
        surprise: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in conn.execute(
            "SELECT stream_id, cell, scale, bin_start, q_value, presence_q FROM surprise "
            "WHERE bin_start >= ? AND q_value IS NOT NULL ORDER BY bin_start",
            (cutoff,),
        ):
            surprise[r["stream_id"]].append(r)
        status = {
            r["stream_id"]: r["status"]
            for r in conn.execute("SELECT stream_id, status FROM sources")
        }
        for sid in stream_ids:
            if not bins.get(sid):  # lagging or stalled feed: show its newest data, dated
                bins[sid] = conn.execute(
                    "SELECT stream_id, cell, scale, bin_start, n, vmin, vmax, vmean FROM bins "
                    "WHERE stream_id = ? AND bin_start = "
                    "(SELECT MAX(bin_start) FROM bins WHERE stream_id = ?)",
                    (sid, sid),
                ).fetchall()
        out = [
            _source_overview(
                sid, cfgs.get(sid), status.get(sid), health.get(sid, {}),
                bins.get(sid, []), surprise.get(sid, []), now,
            )
            for sid in stream_ids
        ]
        n_open = conn.execute("SELECT COUNT(*) FROM alerts WHERE status = 'open'").fetchone()[0]
        return JSONResponse(
            {
                "now": now,
                "lookback": lookback,
                "open_alerts": n_open,
                "reporting": sum(s["state"] == "ok" for s in out),
                "sources": out,
            }
        )

    @app.get("/api/activity.geojson")
    def activity_geojson(
        lookback: int = DEFAULT_LOOKBACK_SECONDS,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> JSONResponse:
        """Where the geocoded streams observed things — one point per
        (stream, cell), sized by observation count. The calm-day map layer."""
        now = _now()
        cutoff = now - lookback
        latest: dict[tuple[str, str], sqlite3.Row] = {}
        totals: dict[tuple[str, str], int] = defaultdict(int)
        for r in conn.execute(
            "SELECT * FROM bins WHERE bin_start >= ? ORDER BY bin_start", (cutoff,)
        ):
            if context.cell_center(r["cell"]) is None:
                continue
            key = (r["stream_id"], r["cell"])
            latest[key] = r
            totals[key] += r["n"]
        stories: dict[tuple[str, str], dict] = {}
        for r in conn.execute(
            "SELECT stream_id, cell, data FROM ("
            " SELECT stream_id, cell, data, ROW_NUMBER() OVER ("
            "  PARTITION BY stream_id, cell ORDER BY rank DESC NULLS LAST, ts DESC) AS rn"
            " FROM context WHERE ts >= ?) WHERE rn = 1",
            (cutoff,),
        ):
            stories[(r["stream_id"], r["cell"])] = json.loads(r["data"])
        # coverage balance (ADR 0002 §G): at most MAP_QUOTA dots per stream per
        # coarse region, the busiest — dense networks don't paint over the map
        keep: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
        for (sid, cell) in latest:
            if cfgs.get(sid) is not None and cfgs[sid].status == "retired":
                continue
            keep[(sid, coarsen(cell, MAP_REGION_RESOLUTION))].append((totals[(sid, cell)], cell))
        shown = {(sid, cell) for (sid, _), cs in keep.items()
                 for _, cell in sorted(cs, reverse=True)[:MAP_QUOTA]}
        features = []
        for (sid, cell), r in latest.items():
            if (sid, cell) not in shown:
                continue
            lat, lon = context.cell_center(cell)  # type: ignore[misc]
            cfg = cfgs.get(sid)
            story = stories.get((sid, cell))
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": {
                        "stream_id": sid,
                        "label": context.display_for(sid, cfg).label,
                        "modality": cfg.modality if cfg else "",
                        "cell": cell,
                        "where": context.fmt_latlon(lat, lon),
                        "n": totals[(sid, cell)],
                        "latest": context.describe_bin(sid, cfg, r),
                        "last": context.observed_at(r, now),
                        "story": evidence.summary(cfg, story) if story else "",
                    },
                }
            )
        return JSONResponse({"type": "FeatureCollection", "features": features})

    @app.get("/api/coverage")
    def coverage(conn: sqlite3.Connection = Depends(get_conn)) -> JSONResponse:
        """Where our own internet probing is thin or absent — shown on the map so
        a quiet country is never mistaken for a watched one (the coverage floor).
        Counts only; target addresses are never exposed."""
        per_cc = {
            r["cc"]: r["n"] for r in conn.execute(
                "SELECT cc, COUNT(*) AS n FROM probe_targets WHERE rejected IS NULL GROUP BY cc"
            )
        }
        feats = []
        for cc, (name, lat, lon) in sorted(countries().items()):
            n = per_cc.get(cc, 0)
            state = "unmonitored" if n == 0 else "thin" if n < THIN_COVERAGE else "ok"
            if state == "ok":
                continue
            feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                          "properties": {"cc": cc, "country": name, "targets": n, "state": state}})
        return JSONResponse({
            "type": "FeatureCollection", "features": feats,
            "summary": {"countries_probed": len(per_cc),
                        "thin": sum(1 for f in feats if f["properties"]["state"] == "thin"),
                        "unmonitored": sum(1 for f in feats if f["properties"]["state"] == "unmonitored")},
        })

    @app.get("/api/cell")
    def cell_detail(
        cell: str,
        lookback: int = DEFAULT_LOOKBACK_SECONDS,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> JSONResponse:
        """Everything observed in one cell over the look-back, per stream."""
        cutoff = _now() - lookback
        latest: dict[str, sqlite3.Row] = {}
        totals: dict[str, int] = defaultdict(int)
        for r in conn.execute(
            "SELECT * FROM bins WHERE cell = ? AND bin_start >= ? ORDER BY bin_start",
            (cell, cutoff),
        ):
            latest[r["stream_id"]] = r
            totals[r["stream_id"]] += r["n"]
        peak: dict[str, sqlite3.Row] = {}
        for r in conn.execute(
            "SELECT stream_id, scale, q_value, presence_q, bin_start FROM surprise "
            "WHERE cell = ? AND bin_start >= ? AND q_value IS NOT NULL",
            (cell, cutoff),
        ):
            best = peak.get(r["stream_id"])
            if best is None or _extremity(r["q_value"], 1.0) > _extremity(best["q_value"], 1.0):
                peak[r["stream_id"]] = r
        streams = []
        for sid in sorted(set(latest) | set(peak)):
            cfg = cfgs.get(sid)
            item: dict[str, object] = {
                "stream_id": sid,
                "label": context.display_for(sid, cfg).label,
                "modality": cfg.modality if cfg else "",
                "observations": totals.get(sid, 0),
            }
            if sid in latest:
                item["latest"] = context.describe_bin(sid, cfg, latest[sid])
                item["latest_at"] = latest[sid]["bin_start"]
            if sid in peak:
                pk = peak[sid]
                item["peak"] = context.surprise_word(pk["q_value"])
                item["peak_at"] = pk["bin_start"]
                item["peak_until"] = pk["bin_start"] + row_seconds(cfg, pk["scale"])
                # the records behind the peak itself — not just the day's top story
                arrival = pk["scale"] == NATIVE_SCALE and cfg is not None and cfg.flavor == "count"
                item["peak_stories"] = _story_dicts(
                    cfg, evidence.top(conn, sid, cell, pk["bin_start"],
                                      max(item["peak_until"], pk["bin_start"] + 1), limit=3,
                                      arrival=arrival)
                )
            recs = evidence.top(conn, sid, cell, cutoff, _now() + 1, limit=5)
            if recs:
                item["stories"] = _story_dicts(cfg, recs)
            streams.append(item)
        return JSONResponse(
            {"cell": cell, "where": context.where(cell), "center": context.cell_center(cell),
             "map_url": context.map_url(cell), "streams": streams}
        )

    return app


def _alert_dict(
    row: sqlite3.Row, conn: sqlite3.Connection, cfgs: dict[str, SourceConfig]
) -> dict[str, object]:
    d = dict(row)
    d["evidence"] = json.loads(row["evidence"])
    title, text, _, _ = format_alert(row, conn, cfgs)
    d.update(title=title, text=text, where=context.where(row["cell"]),
             center=context.cell_center(row["cell"]), map_url=context.map_url(row["cell"]))
    return d


def _story_dicts(cfg: SourceConfig | None, recs: list[dict]) -> list[dict[str, object]]:
    out = []
    for rec in recs:
        url = evidence.link(cfg, rec)
        out.append({"text": evidence.summary(cfg, rec), "url": url,
                    "domain": evidence.domain(url) if url else None, "ts": rec["ts"]})
    return out


def _health_summary(conn: sqlite3.Connection, cutoff: int) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = defaultdict(
        lambda: {"ok": 0, "errors": 0, "last_ok": None, "last_error": None}
    )
    for r in conn.execute(
        "SELECT component, event, COUNT(*) AS n, MAX(ts) AS last FROM health "
        "WHERE ts >= ? GROUP BY component, event",
        (cutoff,),
    ):
        h = out[r["component"]]
        if r["event"] in _OK_EVENTS:
            h["ok"] += r["n"]
            h["last_ok"] = max(h["last_ok"] or 0, r["last"])
        elif r["event"] in _ERROR_EVENTS:
            h["errors"] += r["n"]
    for comp, h in out.items():
        if h["errors"]:
            e = conn.execute(
                "SELECT ts, event, detail FROM health WHERE component = ? AND ts >= ? "
                "AND event IN (%s) ORDER BY ts DESC LIMIT 1" % ",".join("?" * len(_ERROR_EVENTS)),
                (comp, cutoff, *_ERROR_EVENTS),
            ).fetchone()
            h["last_error"] = {"ts": e["ts"], "event": e["event"], "detail": (e["detail"] or "")[:200]}
    return out


def _source_overview(
    sid: str,
    cfg: SourceConfig | None,
    status: str | None,
    health: dict[str, object],
    bins: list[sqlite3.Row],
    surprise: list[sqlite3.Row],
    now: int,
) -> dict[str, object]:
    disp = context.display_for(sid, cfg)
    cadence = cfg.cadence_seconds if cfg else 900
    last_ok = health.get("last_ok")
    last_error = health.get("last_error")
    if last_ok is None and last_error is None:
        state = "waiting"
    elif last_error and (last_ok is None or last_error["ts"] > last_ok):  # type: ignore[index]
        state = "error"
    elif last_ok is not None and now - int(last_ok) > 3 * cadence + 600:  # type: ignore[arg-type]
        state = "stale"
    else:
        state = "ok"

    cells = {b["cell"] for b in bins}
    is_count = cfg is not None and cfg.flavor == "count"
    item: dict[str, object] = {
        "stream_id": sid,
        "label": disp.label,
        "modality": cfg.modality if cfg else "",
        "status": status,
        "cadence_seconds": cadence,
        "state": state,
        "polls_ok": health.get("ok", 0),
        "polls_error": health.get("errors", 0),
        "last_ok": last_ok,
        "last_error": last_error,
        "n_cells": len(cells),
        "n_bins": len(bins),
    }

    if bins:
        if len(cells) == 1 and not is_count:
            latest = bins[-1]
            item["latest"] = context.describe_bin(sid, cfg, latest)
            item["latest_at"] = context.observed_at(latest, now)
            item["spark"] = [
                [b["bin_start"], context.natural_value(b["vmean"], disp)] for b in bins
            ]
        elif is_count:
            total = sum(b["n"] for b in bins)
            per_cell: dict[str, int] = defaultdict(int)
            for b in bins:
                per_cell[b["cell"]] += b["n"]
            busiest, busiest_n = max(per_cell.items(), key=lambda kv: kv[1])
            text = f"{total:,} {disp.unit}".strip() + f" in {len(cells)} cells"
            if disp.max_prefix:
                top = max((b for b in bins if b["vmax"] is not None), key=lambda b: b["vmax"],
                          default=None)
                if top is not None:
                    text += f"; largest {disp.max_prefix}{top['vmax']:.1f} @ {context.where(top['cell'])}"
            elif len(cells) > 1:
                text += f"; busiest {context.where(busiest)} ({busiest_n})"
            item["latest"] = text
            item["latest_at"] = max(context.observed_at(b, now) for b in bins)
            rate: dict[int, float] = defaultdict(float)
            for b in bins:  # events/hour per bin, summed over cells
                rate[b["bin_start"]] += b["n"] * 3600 / bin_width(b["scale"])
            item["spark"] = sorted([t, v] for t, v in rate.items())
        else:  # continuous, many cells (radiation stations, night-light pixels)
            last_per_cell: dict[str, sqlite3.Row] = {}
            for b in bins:
                last_per_cell[b["cell"]] = b
            vals = [
                v for b in last_per_cell.values()
                if (v := context.natural_value(b["vmean"], disp)) is not None
            ]
            if vals:
                med = statistics.median(vals)
                item["latest"] = (
                    f"median {disp.prefix}{context.fmt_number(med, disp.digits)}{disp.unit} "
                    f"over {len(vals)} cells"
                )
            item["latest_at"] = max(context.observed_at(b, now) for b in bins)

    if surprise:
        top = max(surprise, key=lambda r: _extremity(r["q_value"], 1.0))
        item["peak"] = {
            "rarity": context.surprise_word(top["q_value"]),
            "extremity": round(_extremity(top["q_value"], 1.0), 5),
            "where": context.where(top["cell"]),
            "cell": top["cell"],
            "at": top["bin_start"],
        }
        item["n_scored"] = len(surprise)
        item["n_tail"] = sum(_extremity(r["q_value"], 1.0) >= TAIL_EXTREMITY for r in surprise)
        if len(cells) <= 1:
            item["now_rarity"] = context.surprise_word(surprise[-1]["q_value"])
    return item
