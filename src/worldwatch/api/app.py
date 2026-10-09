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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
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
    # a cell straddling ±180° has corners at both edges of the map; unwrap them
    # to one side, or the polygon is drawn as a band around the whole world
    if max(p[0] for p in ring) - min(p[0] for p in ring) > 180:
        ring = [[lng + 360 if lng < 0 else lng, lat] for lng, lat in ring]
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
            "SELECT stream_id, cell, COALESCE(q_detect, q_value) AS q_value, presence_q FROM surprise "
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
            "SELECT bin_start, COALESCE(q_detect, q_value) AS q_value, q_value AS pit, "
            "presence_q, n_obs FROM surprise "
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
        bbox: str | None = None,
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
        view = parse_bbox(bbox)
        if view is not None:
            rows = [r for r in rows if in_view(r["cell"], view)]
        return JSONResponse({"alerts": [_alert_dict(r, conn, cfgs) for r in rows]})

    @app.get("/api/alerts/{alert_id}")
    def alert_detail(alert_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> JSONResponse:
        row = conn.execute("SELECT * FROM alerts WHERE alert_id = ?", (alert_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="alert not found")
        return JSONResponse(_alert_dict(row, conn, cfgs))

    @app.get("/digest")
    @app.get("/digest/{week_end}")
    def digest_page(week_end: int | None = None, conn: sqlite3.Connection = Depends(get_conn)) -> HTMLResponse:
        """The weekly report (latest, or the week ending at week_end)."""
        from worldwatch.api.digest import render_digest

        page = render_digest(conn, week_end)
        if page is None:
            raise HTTPException(status_code=404, detail="no weekly report yet")
        return HTMLResponse(page)

    @app.get("/api/nursery")
    def nursery_json(conn: sqlite3.Connection = Depends(get_conn)) -> JSONResponse:
        """Each source's calibration verdict: active (alerts on its q-values),
        nursery (not yet proven) or quarantined (drifted), and why."""
        from worldwatch.layer0 import nursery

        return JSONResponse({"criteria": {
            "window_days": nursery.WINDOW_DAYS, "recent_days": nursery.RECENT_DAYS,
            "min_n": nursery.MIN_N, "min_span_days": nursery.MIN_SPAN_DAYS,
            "tv_pass": nursery.TV_PASS, "tv_quarantine": nursery.TV_QUARANTINE, "tail": nursery.TAIL},
            "streams": nursery.latest(conn)})

    @app.get("/api/resources")
    def resources_json(conn: sqlite3.Connection = Depends(get_conn)) -> JSONResponse:
        """What Worldwatch costs the machine: memory, CPU, disk, network, and
        each source's downloads; with warnings when one is disproportionate."""
        from worldwatch import usage

        return JSONResponse(usage.report(conn, _now()))

    @app.get("/resources")
    def resources_page(conn: sqlite3.Connection = Depends(get_conn)) -> HTMLResponse:
        from worldwatch import usage
        from worldwatch.api.digest import PAGE, markdown

        text = "\n".join(usage.summary_lines(usage.report(conn, _now()), top=40))
        return HTMLResponse(PAGE.format(title="WW resources", nav='<a href="/api/resources">JSON</a>',
                                        analysis="", digest=markdown(text)))

    @app.get("/alert/{alert_id}")
    def alert_report(alert_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> HTMLResponse:
        """The alert's full report (a push's tap opens it)."""
        from worldwatch.api.report import render_report

        page = render_report(conn, alert_id, cfgs)
        if page is None:
            raise HTTPException(status_code=404, detail="alert not found")
        return HTMLResponse(page)

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


    @app.post("/api/feedback/{alert_id}/{verdict}/{sig}")
    def feedback(
        alert_id: int, verdict: str, sig: str, conn: sqlite3.Connection = Depends(get_conn)
    ) -> JSONResponse:
        """A push's Useful / Not useful button (signed per alert and verdict; the
        only write the public dashboard lets through)."""
        from worldwatch.api import feedback as fb

        if not fb.verify(conn, alert_id, verdict, sig):
            raise HTTPException(status_code=403, detail="bad signature")
        if not fb.record(conn, alert_id, verdict):
            raise HTTPException(status_code=404, detail="alert not found")
        return JSONResponse({"alert_id": alert_id, "feedback": verdict})

    @app.get("/api/overview")
    def overview(
        lookback: int = DEFAULT_LOOKBACK_SECONDS,
        bbox: str | None = None,
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
            "SELECT stream_id, cell, scale, bin_start, COALESCE(q_detect, q_value) AS q_value, "
            "q_value AS pit, presence_q FROM surprise "
            "WHERE bin_start >= ? AND q_value IS NOT NULL ORDER BY bin_start",
            (cutoff,),
        ):
            surprise[r["stream_id"]].append(r)
        status = {
            r["stream_id"]: r["status"]
            for r in conn.execute("SELECT stream_id, status FROM sources")
        }
        view = parse_bbox(bbox)
        for sid in stream_ids:
            if not bins.get(sid):  # lagging or stalled feed: its newest data, dated
                bins[sid] = conn.execute(
                    "SELECT stream_id, cell, scale, bin_start, n, vmin, vmax, vmean FROM bins "
                    "WHERE stream_id = ? AND bin_start = "
                    "(SELECT MAX(bin_start) FROM bins WHERE stream_id = ?)",
                    (sid, sid),
                ).fetchall()
        if view is not None:  # only what lies in the map view; non-spatial rows always count
            bins = {sid: [b for b in rs if in_view(b["cell"], view)] for sid, rs in bins.items()}
            surprise = {sid: [r for r in rs if in_view(r["cell"], view)] for sid, rs in surprise.items()}
        # where each row takes the map — from the same rows the list shows, so
        # with a view the peak is the one in view, not one across the continent
        places = {sid: _place(bins.get(sid, []), surprise.get(sid, [])) for sid in stream_ids}
        out = []
        hidden = 0
        for sid in stream_ids:
            if view is not None and not places[sid]["global"] and not bins.get(sid):
                hidden += 1
                continue
            item = _source_overview(
                sid, cfgs.get(sid), status.get(sid), health.get(sid, {}),
                bins.get(sid, []), surprise.get(sid, []), now,
            )
            item.update(places[sid])
            out.append(item)
        n_open = conn.execute("SELECT COUNT(*) FROM alerts WHERE status = 'open'").fetchone()[0]
        return JSONResponse(
            {
                "now": now,
                "lookback": lookback,
                "open_alerts": n_open,
                "reporting": sum(s["state"] == "ok" for s in out),
                "sources": out,
                "out_of_view": hidden,
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
            "SELECT stream_id, scale, COALESCE(q_detect, q_value) AS q_value, presence_q, "
            "bin_start FROM surprise "
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


def parse_bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    """'west,south,east,north' in degrees (as MapLibre's getBounds gives, which
    may run past ±180 with world copies) → normalized, or None for no filter."""
    if not bbox:
        return None
    try:
        w, s_, e, n = (float(x) for x in bbox.split(","))
    except ValueError:
        return None
    if e - w >= 360:
        w, e = -180.0, 180.0  # the whole width is in view
    else:
        w, e = ((w + 180) % 360) - 180, ((e + 180) % 360) - 180
    return w, max(s_, -90.0), e, min(n, 90.0)


def in_view(cell: str, view: tuple[float, float, float, float]) -> bool:
    """Is the cell's centre inside the view? Non-spatial cells (GLOBAL, an
    entity) are everywhere, so always in view."""
    c = context.cell_center(cell)
    if c is None:
        return True
    lat, lon = c
    w, s_, e, n = view
    if not s_ <= lat <= n:
        return False
    return w <= lon <= e if w <= e else (lon >= w or lon <= e)  # across the antimeridian


def _place(bins: list[sqlite3.Row], surprise: list[sqlite3.Row]) -> dict[str, object]:
    """Where a source's row should take the map: its peak if that is unusual
    and located, else the extent of what it reported; `global` if it isn't
    tied to any place (prices, world traffic, Wikipedia)."""
    pts = [c for b in bins if (c := context.cell_center(b["cell"])) is not None]
    peak_center = None
    if surprise:
        top = max(surprise, key=lambda r: _extremity(r["q_value"], 1.0))
        if _extremity(top["q_value"], 1.0) >= 0.9:
            peak_center = context.cell_center(top["cell"])
    extent = None
    if pts:
        lats, lons = [p[0] for p in pts], [p[1] for p in pts]
        extent = [min(lons), min(lats), max(lons), max(lats)]
    return {"center": peak_center, "extent": extent, "global": not pts and bool(bins)}


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
        # calibration is a property of the randomized PIT, not of the detection q
        item["n_tail"] = sum(_extremity(r["pit"], 1.0) >= TAIL_EXTREMITY for r in surprise)
        if len(cells) <= 1:
            item["now_rarity"] = context.surprise_word(surprise[-1]["q_value"])
    return item
