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
import time
from collections.abc import Iterator
from pathlib import Path

import h3
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from worldwatch.db import connect

_STATIC = Path(__file__).parent / "static"
DEFAULT_LOOKBACK_SECONDS = 24 * 3600


def _extremity(q_value: float | None, presence_q: float) -> float:
    """Tail depth 0..1 (0 = unremarkable). Data rows use q_value; silence rows
    (q_value NULL) use presence_q."""
    if q_value is not None:
        return max(q_value, 1.0 - q_value)
    return presence_q


def _cell_polygon(cell: str) -> list[list[float]] | None:
    """GeoJSON [lng, lat] ring for an H3 cell, or None if not an H3 cell."""
    if not h3.is_valid_cell(cell):
        return None
    ring = [[lng, lat] for lat, lng in h3.cell_to_boundary(cell)]
    ring.append(ring[0])  # close the ring
    return ring


class Label(BaseModel):
    label: str  # true | false_positive | unclear


def create_app(db_path: Path, now_fn: object = time.time) -> FastAPI:
    app = FastAPI(title="Worldwatch", docs_url="/api/docs")

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _now() -> int:
        return int(now_fn())  # type: ignore[operator]

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_STATIC / "index.html")

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
                    "surprise": round(max_surprise[cell], 4),
                    "n_streams": len(streams[cell]),
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
        return JSONResponse({"alerts": [_alert_dict(r) for r in rows]})

    @app.get("/api/alerts/{alert_id}")
    def alert_detail(alert_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> JSONResponse:
        row = conn.execute("SELECT * FROM alerts WHERE alert_id = ?", (alert_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="alert not found")
        return JSONResponse(_alert_dict(row))

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

    return app


def _alert_dict(row: sqlite3.Row) -> dict[str, object]:
    d = dict(row)
    d["evidence"] = json.loads(row["evidence"])
    return d
