"""Evidence store: what each observation *was*, for the person reading an alert.

The detection path sees only the interchange contract; this store holds the
slim human-readable record next to it — a quake's place and USGS page, a
weather alert's headline, a news event's action, place and article link — so
a 3 a.m. push can say what happened from data already downloaded, instead of
sending its reader off to search the web.

Guardrails: never read by Layer 0/1 or the alert engine; a fixed byte budget
(WW_CONTEXT_BUDGET_MB) with the oldest records evicted first; only fields a
stanza's [context] table names are kept, and never personal data.

Stanza shape (geojson feeds name properties; GDELT names TSV columns):

    [usgs_seismic.context]
    fields = ["place", "mag", "url", "tsunami", "alert"]
    rank   = "mag"                    # which record leads a cell's summary
    text   = "M{mag}| {place}|, depth {depth_km:.0f} km"
                                      # one-line summary; "|" separates pieces,
                                      # each shown only if all its fields exist
    link   = "url"                    # field holding the source page
"""

from __future__ import annotations

import json
import sqlite3
import string
from typing import Any
from urllib.parse import urlparse

from worldwatch.config.loader import SourceConfig
from worldwatch.ingest.models import Observation

DEFAULT_BUDGET_MB = 2048
MAX_FIELD_CHARS = 400
_PRUNE_CHUNK = 5000


def spec(cfg: SourceConfig) -> dict[str, Any] | None:
    ctx = cfg.extra.get("context")
    return dict(ctx) if isinstance(ctx, dict) else None


def pick(cfg: SourceConfig, source: dict[str, Any]) -> dict[str, object] | None:
    """The stanza's [context] fields from one raw record (dropping empties)."""
    s = spec(cfg)
    if s is None:
        return None
    out: dict[str, object] = {}
    for name in s.get("fields", []):
        v = source.get(name)
        if v is None or v == "":
            continue
        if isinstance(v, str):
            v = v.strip()[:MAX_FIELD_CHARS]
        elif not isinstance(v, (int, float, bool)):
            continue
        out[name.lstrip("@")] = v  # NWS "@id" → "id"
    return out or None


def put(conn: sqlite3.Connection, o: Observation) -> None:
    ctx = dict(o.context or {})
    rank_value = ctx.pop("_rank", None)
    data = json.dumps(ctx, separators=(",", ":"), ensure_ascii=False)
    conn.execute(
        "INSERT OR IGNORE INTO context (stream_id, cell, ts, rank, size, data) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (o.stream_id, o.cell, o.ts, _num(rank_value), len(data.encode()), data),
    )


def with_rank(cfg: SourceConfig, ctx: dict[str, object] | None) -> dict[str, object] | None:
    """Attach the stanza's rank value as `_rank` (stored in its own column)."""
    if ctx is None:
        return None
    s = spec(cfg) or {}
    field = s.get("rank")
    if field and field.lstrip("@") in ctx:
        ctx = {**ctx, "_rank": ctx[field.lstrip("@")]}
    return ctx


def prune(conn: sqlite3.Connection, budget_bytes: int) -> int:
    """Evict the oldest records until the store fits its budget. Returns rows removed."""
    total = conn.execute("SELECT COALESCE(SUM(size), 0) FROM context").fetchone()[0]
    removed = 0
    while total > budget_bytes:
        rows = conn.execute(
            "SELECT rowid, size FROM context ORDER BY rowid LIMIT ?", (_PRUNE_CHUNK,)
        ).fetchall()
        if not rows:
            break
        cut, freed = rows[-1][0], 0
        for rowid, size in rows:  # stop as soon as we're under budget
            freed += size
            if total - freed <= budget_bytes:
                cut = rowid
                break
        n = conn.execute("DELETE FROM context WHERE rowid <= ?", (cut,)).rowcount
        removed += n
        total = conn.execute("SELECT COALESCE(SUM(size), 0) FROM context").fetchone()[0]
    conn.commit()
    return removed


def top(
    conn: sqlite3.Connection,
    stream_id: str,
    cell: str,
    t0: int,
    t1: int,
    limit: int = 3,
) -> list[dict[str, object]]:
    """Highest-ranked records for (stream, cell) in [t0, t1), one per link."""
    rows = conn.execute(
        "SELECT ts, data FROM context WHERE stream_id = ? AND cell = ? AND ts >= ? AND ts < ? "
        "ORDER BY rank DESC NULLS LAST, ts DESC LIMIT ?",
        (stream_id, cell, t0, t1, limit * 8),
    ).fetchall()
    out, links = [], set()
    for r in rows:
        d = json.loads(r[1])
        key = d.get("url") or d.get("id") or r[1]
        if key in links:
            continue  # GDELT codes several events per article
        links.add(key)
        d["ts"] = r[0]
        out.append(d)
        if len(out) >= limit:
            break
    return out


def summary(cfg: SourceConfig | None, rec: dict[str, object]) -> str:
    """One line from the stanza's `text` template; unknown keys render empty."""
    s = (spec(cfg) if cfg else None) or {}
    template = str(s.get("text", ""))
    if not template:
        return ", ".join(f"{k} {v}" for k, v in rec.items() if not k.startswith("_") and k != "ts")
    fmt = _Formatter()
    parts = []
    for piece in template.split("|"):  # a piece renders only if all its fields exist
        names = [f for _, f, _, _ in fmt.parse(piece) if f]
        if all(rec.get(n) not in (None, "") for n in names):
            parts.append(fmt.vformat(piece, (), rec))
    return " ".join("".join(parts).split()).strip(" ,-:")


def link(cfg: SourceConfig | None, rec: dict[str, object]) -> str | None:
    s = (spec(cfg) if cfg else None) or {}
    v = rec.get(str(s.get("link", "url")).lstrip("@"))
    return v if isinstance(v, str) and v.startswith(("http://", "https://")) else None


def domain(url: str) -> str:
    host = urlparse(url).hostname or url
    return host.removeprefix("www.")


class _Formatter(string.Formatter):
    """Missing keys and mismatched format specs render empty, never raise."""

    def get_value(self, key, args, kwargs):  # type: ignore[no-untyped-def]
        return kwargs.get(key, "") if isinstance(key, str) else ""

    def format_field(self, value, format_spec):  # type: ignore[no-untyped-def]
        try:
            return super().format_field(value, format_spec)
        except (ValueError, TypeError):
            return "" if value == "" else str(value)


def _num(v: object) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
