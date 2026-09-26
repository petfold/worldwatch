"""Push notifications for opened alerts.

ntfy is the default (self-hostable — the operator prefers self-hosting over
proprietary push). Config comes from the environment; if unconfigured the
notifier is a no-op that records a health row, so the pipeline never fails for
lack of a push channel. Telegram can be added behind the same `notify_alert`
seam later (see doc/OPERATOR-TODO.md).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass

import httpx

from worldwatch import evidence as evstore
from worldwatch.api import context
from worldwatch.layer0.native import NATIVE_SCALE, row_seconds
from worldwatch.config.loader import SourceConfig
from worldwatch.instrument import record_health

MAX_EVIDENCE_LINES = 8
STORIES_PER_SIGNAL = 2
MAX_MESSAGE_BYTES = 3900  # ntfy's limit is 4096; leave room for the footer
MAX_ACTIONS = 3  # ntfy allows three buttons


@dataclass(frozen=True)
class NtfyConfig:
    server: str
    topic: str
    token: str | None = None
    dashboard_url: str | None = None  # public dashboard; the push deep-links into it

    @classmethod
    def from_env(cls) -> NtfyConfig | None:
        topic = os.environ.get("WW_NTFY_TOPIC")
        if not topic:
            return None
        return cls(
            server=os.environ.get("WW_NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
            topic=topic,
            token=os.environ.get("WW_NTFY_TOKEN"),
            dashboard_url=(os.environ.get("WW_DASHBOARD_URL") or "").rstrip("/") or None,
        )


def format_alert(
    alert: sqlite3.Row,
    conn: sqlite3.Connection | None = None,
    sources: dict[str, SourceConfig] | None = None,
) -> tuple[str, str, int, list[str]]:
    """(title, message, priority, tags) for an alert row.

    With `conn`, each evidence line also says what was observed (from the
    consolidated bin) — context for the reader only; the alert itself was
    opened on q_values alone."""
    evidence = json.loads(alert["evidence"])
    modalities = sorted({e["modality"] for e in evidence})
    streams = [e["stream_id"] for e in evidence]
    severity = float(alert["severity"])
    sources = sources or {}

    level = "SEVERE" if severity >= 0.9 else "HIGH" if severity >= 0.7 else "notable"
    # ASCII only (HTTP header)
    title = f"Worldwatch {level} {severity:.2f} - {context.where(alert['cell'])}"
    is_source_alert = any(e.get("kind") == "source_alert" for e in evidence)
    is_silence = (
        not is_source_alert and all(e.get("q_value") is None for e in evidence) and bool(evidence)
    )
    from worldwatch.alerts.engine import policy

    single = {e["stream_id"] for e in evidence}
    if is_source_alert:
        kind = "source alert"
    elif len(single) == 1 and policy(sources.get(next(iter(single)))).get("single_source"):
        n = len({e.get("cell") for e in evidence})
        kind = f"single-network surprise ({n} independent sensor{'s' if n != 1 else ''})"
    else:
        kind = "multi-source silence" if is_silence else "corroborated surprise"

    lines = [
        f"{kind} (severity {severity:.2f})",
        f"region {context.where(alert['cell'])} ({alert['cell']}) @ scale {alert['scale']}",
        f"{len(streams)} signals across {len(modalities)} modalities: {', '.join(modalities)}",
    ]
    starts = [e["bin_start"] for e in evidence if e.get("bin_start") is not None]
    if starts:
        lines.append(f"latest bin {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(max(starts)))}")
    lines.append("")
    for e in evidence[:MAX_EVIDENCE_LINES]:
        lines.append(_evidence_line(e, conn, sources))
        for rec in _stories(e, conn, sources):
            cfg = sources.get(e["stream_id"])
            url = evstore.link(cfg, rec)
            lines.append(f"    > {evstore.summary(cfg, rec)}" + (f" [{evstore.domain(url)}]" if url else ""))
    if len(evidence) > MAX_EVIDENCE_LINES:
        lines.append(f"+{len(evidence) - MAX_EVIDENCE_LINES} more signals")
    news = _news_in_area(alert, conn, sources)
    if news:
        lines.append("")
        lines.append("news in the area (context, not evidence):")
        for cfg, rec in news:
            url = evstore.link(cfg, rec)
            lines.append(f"  > {evstore.summary(cfg, rec)}" + (f" [{evstore.domain(url)}]" if url else ""))
    lines.append("")
    lines.append(f"streams: {', '.join(streams[:6])}")
    message = "\n".join(lines)
    while len(message.encode()) > MAX_MESSAGE_BYTES and len(lines) > 4:
        lines.pop(-3)  # drop the last detail line, keep header and footer
        message = "\n".join(lines[:-2] + ["(truncated)"] + lines[-2:])

    priority = 5 if severity >= 0.9 else 4 if severity >= 0.7 else 3
    tags = ["mute"] if is_silence else ["rotating_light"]
    return title, message, priority, tags


def _stories(
    e: dict, conn: sqlite3.Connection | None, sources: dict[str, SourceConfig]
) -> list[dict[str, object]]:
    """Evidence-store records behind one signal: same stream and cell, inside
    the scored bin — what actually happened, already downloaded."""
    if conn is None or not e.get("cell") or e.get("bin_start") is None:
        return []
    cfg = sources.get(e["stream_id"])
    if cfg is None or evstore.spec(cfg) is None:
        return []
    t0 = int(e["bin_start"])
    scale = int(e.get("scale") or 0)
    width = e.get("bin_seconds")
    if width is None:
        width = row_seconds(cfg, scale)
    if e.get("kind") == "source_alert":
        width = 0
    arrival = scale == NATIVE_SCALE and cfg.flavor == "count"
    return evstore.top(conn, e["stream_id"], e["cell"], t0, t0 + max(int(width), 1),
                       STORIES_PER_SIGNAL, arrival=arrival)


NEWS_WINDOW_SECONDS = 3 * 3600


def _news_in_area(
    alert: sqlite3.Row, conn: sqlite3.Connection | None, sources: dict[str, SourceConfig]
) -> list[tuple[SourceConfig, dict[str, object]]]:
    """Stories from context-role streams (news) in the alert's region over the
    last few hours — what people are already saying there, if anything."""
    import h3

    from worldwatch.alerts.engine import policy

    region = alert["cell"]
    if conn is None or not h3.is_valid_cell(region):
        return []
    out: list[tuple[SourceConfig, dict[str, object]]] = []
    t1 = int(alert["opened_at"]) + 1
    for sid, cfg in sources.items():
        if policy(cfg).get("role") != "context" or evstore.spec(cfg) is None:
            continue
        res = int(cfg.geocode.get("h3_resolution", 3))
        base = h3.get_resolution(region)
        cells = [region] if res <= base else list(h3.cell_to_children(region, res))
        recs = []
        for c in cells:
            recs += evstore.top(conn, sid, c, t1 - NEWS_WINDOW_SECONDS, t1, limit=3)
        recs.sort(key=lambda r: -float(r.get("mentions") or 0))
        out += [(cfg, r) for r in recs[:STORIES_PER_SIGNAL + 1]]
    return out


def story_links(
    alert: sqlite3.Row, conn: sqlite3.Connection | None, sources: dict[str, SourceConfig] | None
) -> list[tuple[str, str]]:
    """(button label, url) for the alert's top source pages, best first."""
    sources = sources or {}
    out: list[tuple[str, str]] = []
    for e in json.loads(alert["evidence"]):
        cfg = sources.get(e["stream_id"])
        for rec in _stories(e, conn, sources):
            url = evstore.link(cfg, rec)
            if url and all(url != u for _, u in out):
                out.append((evstore.domain(url)[:24], url))
    return out


def _evidence_line(
    e: dict, conn: sqlite3.Connection | None, sources: dict[str, SourceConfig]
) -> str:
    """'- Earthquakes M4.5+ (USGS) [physical] 1-in-2,500 high: 1 quake, max M6.6 @ 21.3S 167.9E'"""
    cfg = sources.get(e["stream_id"])
    label = context.display_for(e["stream_id"], cfg).label
    head = f"- {label} [{e['modality']}]"
    if e.get("kind") == "source_alert":
        return f"{head} issued by the source" + (
            f" @ {context.where(e['cell'])}" if e.get("cell") and context.cell_center(e["cell"]) else ""
        )
    if e.get("q_value") is None:
        return f"{head} silent (presence {e['presence_q']:.2f})"
    odds, direction = context.rarity(e["q_value"])
    line = f"{head} {odds} {direction}"
    if conn is not None and e.get("cell"):
        row = context.bin_row(conn, e["stream_id"], e["cell"], e.get("scale"), e.get("bin_start"))
        if row is not None:
            line += f": {context.describe_bin(e['stream_id'], cfg, row)}"
    if e.get("cell") and context.cell_center(e["cell"]):
        line += f" @ {context.where(e['cell'])}"
    return line


def _ascii(s: str) -> str:
    """HTTP header values must be latin-1; keep button labels plain ASCII."""
    return s.encode("ascii", "ignore").decode().replace(",", " ").replace(";", " ") or "Link"


async def send_ntfy(
    client: httpx.AsyncClient,
    cfg: NtfyConfig,
    alert: sqlite3.Row,
    conn: sqlite3.Connection | None = None,
    sources: dict[str, SourceConfig] | None = None,
) -> bool:
    """Publish one alert to ntfy. Returns True on success."""
    title, message, priority, tags = format_alert(alert, conn, sources)
    headers = {
        "Title": title,
        "Priority": str(priority),
        "Tags": ",".join(tags),
    }
    map_url = context.map_url(alert["cell"])
    buttons = story_links(alert, conn, sources)  # tap straight to the source pages
    if cfg.dashboard_url:
        # tapping the push opens this alert on the dashboard; the map is a button
        headers["Click"] = f"{cfg.dashboard_url}/?alert={alert['alert_id']}"
        if map_url:
            buttons.append(("Map", map_url))
    elif map_url:
        headers["Click"] = map_url  # no dashboard configured: open the region on a map
    if buttons:
        headers["Actions"] = "; ".join(
            f"view, {_ascii(label)}, {url}" for label, url in buttons[:MAX_ACTIONS]
        )
    if cfg.token:
        headers["Authorization"] = f"Bearer {cfg.token}"
    resp = await client.post(
        f"{cfg.server}/{cfg.topic}", content=message.encode(), headers=headers, timeout=15.0
    )
    resp.raise_for_status()
    return True


async def notify_alerts(
    conn: sqlite3.Connection,
    alert_ids: list[int],
    client: httpx.AsyncClient | None = None,
    cfg: NtfyConfig | None = None,
    sources: dict[str, SourceConfig] | None = None,
) -> int:
    """Push each alert id. Returns the count delivered. No-op (health-logged)
    when no channel is configured."""
    if not alert_ids:
        return 0
    cfg = cfg or NtfyConfig.from_env()
    if cfg is None:
        record_health(conn, "notify", "unconfigured", f"pending={len(alert_ids)}")
        return 0

    rows = conn.execute(
        f"SELECT * FROM alerts WHERE alert_id IN ({','.join('?' * len(alert_ids))})",
        alert_ids,
    ).fetchall()

    owns_client = client is None
    client = client or httpx.AsyncClient()
    delivered = 0
    try:
        for alert in rows:
            try:
                if await send_ntfy(client, cfg, alert, conn, sources):
                    delivered += 1
            except httpx.HTTPError as e:
                record_health(conn, "notify", "push_error", f"alert={alert['alert_id']}: {e}")
    finally:
        if owns_client:
            await client.aclose()
    record_health(conn, "notify", "ok", f"delivered={delivered}")
    return delivered
