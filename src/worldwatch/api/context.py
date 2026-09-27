"""Human-readable context for pushes and the dashboard (presentation only).

Detection never reads this: alerts open on q_values alone (the interchange
contract). This module turns the cascade's consolidated bins back into units a
person recognises — quakes and magnitudes, dollars, events — so a push or a
dashboard card says what was observed, not only how surprising it was.
Per-source wording comes from the stanza's optional `[display]` table.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

import h3

from worldwatch.cascade.bins import bin_width
from worldwatch.config.loader import SourceConfig
from worldwatch.config.places import cell_place


@dataclass(frozen=True)
class Display:
    label: str
    unit: str = ""
    prefix: str = ""
    digits: int = 0
    inverse: str = ""  # "expm1" / "exp" undo the parser's log1p / log transform
    percent: bool = False  # value is a 0..1 fraction; show as %
    max_prefix: str = ""  # count streams: also show the bin max (e.g. "M" magnitude)
    about: str = ""  # what the source measures, in plain words (alert reports)


def display_for(stream_id: str, cfg: SourceConfig | None) -> Display:
    d = dict(cfg.extra.get("display", {})) if cfg is not None else {}
    return Display(
        label=str(d.get("label", stream_id)),
        unit=str(d.get("unit", "")),
        prefix=str(d.get("prefix", "")),
        digits=int(d.get("digits", 0)),
        inverse=str(d.get("inverse", "")),
        percent=bool(d.get("percent", False)),
        max_prefix=str(d.get("max_prefix", "")),
        about=str(d.get("about", "")),
    )


def natural_value(v: float | None, disp: Display) -> float | None:
    """A bin statistic back in the source's own units."""
    if v is None:
        return None
    if disp.inverse == "expm1":
        v = math.expm1(v)
    elif disp.inverse == "exp":
        v = math.exp(v)
    if disp.percent:
        v *= 100.0
    return v


def fmt_number(v: float, digits: int = 0) -> str:
    if abs(v) >= 1e6:
        return f"{v / 1e6:.1f}M"
    return f"{v:,.{digits}f}"


def describe_bin(stream_id: str, cfg: SourceConfig | None, row: sqlite3.Row | dict) -> str:
    """One consolidated bin in plain words: '4 quakes, max M5.1', '$113,245'."""
    disp = display_for(stream_id, cfg)
    if cfg is not None and cfg.flavor == "count":
        unit = disp.unit[:-1] if row["n"] == 1 and disp.unit.endswith("s") else disp.unit
        text = f"{row['n']} {unit}".strip()
        if disp.max_prefix and row["vmax"] is not None:
            text += f", max {disp.max_prefix}{row['vmax']:.1f}"
        return text
    v = natural_value(row["vmean"], disp)
    if v is None:
        return f"{row['n']} obs"
    return f"{disp.prefix}{fmt_number(v, disp.digits)}{disp.unit}"


RARITY_CAP = 1e6  # beyond ~1-in-a-million the model's tail is not trustworthy as a number


def rarity(q_value: float) -> tuple[str, str]:
    """('1-in-2,500', 'high'|'low') for a two-sided tail quantile."""
    q = min(max(q_value, 1e-12), 1 - 1e-12)
    tail = min(q, 1 - q)
    odds = "over 1-in-1M" if 1 / tail >= RARITY_CAP else f"1-in-{fmt_number(1 / tail)}"
    return odds, "high" if q > 0.5 else "low"


def rarity_phrase(q_value: float) -> str:
    """'unusually high (1 in 2,500)'."""
    odds, direction = rarity(q_value)
    return f"unusually {direction} ({odds.replace('1-in-', '1 in ')})"


def surprise_word(q_value: float) -> str:
    """Plain summary for calm and not-calm alike: 'typical', 'unusual …', 'rare …'."""
    ext = max(q_value, 1 - q_value)
    if ext < 0.9:
        return "typical"
    odds, direction = rarity(q_value)
    return f"{'rare' if ext >= 0.99 else 'unusual'}, {odds} {direction}"


def extremity(q_value: float | None, presence_q: float) -> float:
    """Tail depth 0.5..1 (silence rows, with NULL q_value, use presence_q)."""
    if q_value is not None:
        return max(q_value, 1.0 - q_value)
    return presence_q


def cell_center(cell: str) -> tuple[float, float] | None:
    if not h3.is_valid_cell(cell):
        return None
    return h3.cell_to_latlng(cell)


def fmt_latlon(lat: float, lon: float) -> str:
    return f"{abs(lat):.1f}{'N' if lat >= 0 else 'S'} {abs(lon):.1f}{'E' if lon >= 0 else 'W'}"


def where(cell: str) -> str:
    """'35.7N 139.7E' for an H3 cell, else the literal cell (GLOBAL, entity)."""
    c = cell_center(cell)
    return fmt_latlon(*c) if c else cell


def place(cell: str) -> str:
    """'Japan (35.7N 139.7E)', 'Sea of Japan (38.5N 135.0E)'; 'worldwide' for GLOBAL."""
    c = cell_center(cell)
    if c is None:
        return "worldwide" if cell == "GLOBAL" else cell
    name = cell_place(cell)
    return f"{name} ({fmt_latlon(*c)})" if name else fmt_latlon(*c)


def place_names(cells: list[str], limit: int = 2) -> str:
    """The distinct place names of some cells, in order: 'Finland, Estonia +1'."""
    names: list[str] = []
    for c in cells:
        n = cell_place(c) or ("worldwide" if c == "GLOBAL" else "")
        if n and n not in names:
            names.append(n)
    more = len(names) - limit
    return ", ".join(names[:limit]) + (f" +{more}" if more > 0 else "")


def map_url(cell: str) -> str | None:
    c = cell_center(cell)
    if c is None:
        return None
    zoom = max(3, min(10, h3.get_resolution(cell) + 3))
    return f"https://www.openstreetmap.org/?mlat={c[0]:.3f}&mlon={c[1]:.3f}#map={zoom}/{c[0]:.3f}/{c[1]:.3f}"


def bin_row(
    conn: sqlite3.Connection, stream_id: str, cell: str, scale: int | None, start: int | None
) -> sqlite3.Row | None:
    """The bin a surprise row scored, else the latest bin at or before it (the
    exact bin may since have folded into a coarser scale)."""
    if scale is not None and start is not None:
        row = conn.execute(
            "SELECT * FROM bins WHERE stream_id = ? AND cell = ? AND scale = ? AND bin_start = ?",
            (stream_id, cell, scale, start),
        ).fetchone()
        if row is not None:
            return row
    return conn.execute(
        "SELECT * FROM bins WHERE stream_id = ? AND cell = ? AND bin_start <= ? "
        "ORDER BY bin_start DESC LIMIT 1",
        (stream_id, cell, start if start is not None else 2**62),
    ).fetchone()


TYPICAL_SECONDS = 7 * 86400


def typical(conn: sqlite3.Connection, stream_id: str, cfg: SourceConfig | None, cell: str,
            before: int | None) -> str | None:
    """The stream's usual level at this cell: the median of its bins (any scale: the
    cascade's bins do not overlap) over the week before, in its own units ('0.10
    µSv/h'); None for counts or without history."""
    if cfg is None or cfg.flavor == "count" or before is None:
        return None
    vals = sorted(r[0] for r in conn.execute(
        "SELECT vmean FROM bins WHERE stream_id = ? AND cell = ? AND bin_start < ? "
        "AND bin_start >= ? AND vmean IS NOT NULL",
        (stream_id, cell, before, before - TYPICAL_SECONDS)))
    if len(vals) < 3:
        return None
    disp = display_for(stream_id, cfg)
    v = natural_value(vals[len(vals) // 2], disp)
    return None if v is None else f"{disp.prefix}{fmt_number(v, disp.digits)}{disp.unit}"


def observed_at(row: sqlite3.Row | dict, now: int) -> int:
    """When a bin's data is from, for display. Fine bins: their end (capped at
    now). Coarse bins (wider than a day, e.g. a stalled feed's months-old data)
    only bound the observation, so report the conservative start."""
    width = bin_width(int(row["scale"]))
    start = int(row["bin_start"])
    return min(start + width, now) if width <= 86400 else start
