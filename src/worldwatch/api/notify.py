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
    per_day: int = 2  # the push budget: about this many a day, the most serious (never 2x in 24 h)
    min_score: float = 8.0  # and never an alert scoring less (alerts.engine.alert_score)
    extreme_score: float = 15.0  # extreme (may wake, priority 5): at least this, confirmed
    extreme_per_week: int = 1  # and at most this many a week
    silent_until: int | None = None  # before this (epoch s), every push is silent (priority <= 2)

    def silent(self, now: float | None = None) -> bool:
        return self.silent_until is not None and (time.time() if now is None else now) < self.silent_until

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
            per_day=int(os.environ.get("WW_PUSH_PER_DAY", "2")),
            min_score=float(os.environ.get("WW_PUSH_MIN_SCORE", "8")),
            extreme_score=float(os.environ.get("WW_PUSH_EXTREME_SCORE", "15")),
            extreme_per_week=int(os.environ.get("WW_PUSH_EXTREME_PER_WEEK", "1")),
            silent_until=_parse_until(os.environ.get("WW_PUSH_SILENT_UNTIL")),
        )


def _parse_until(value: str | None) -> int | None:
    """WW_PUSH_SILENT_UNTIL: an ISO date or time (UTC), or epoch seconds."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    from datetime import datetime, timezone

    t = datetime.fromisoformat(value)
    return int((t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp())


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
    extreme: bool | None = None,
) -> bool:
    """Publish one alert to ntfy. Returns True on success. extreme: whether it may wake
    the operator (priority 5); others are pushed at 3 at most (None: as formatted)."""
    title, message, priority, tags = format_alert(alert, conn, sources)
    if extreme is not None:
        priority = 5 if extreme else min(priority, 3)
    if cfg.silent():  # ntfy 2: no sound, no vibration; still listed
        priority = min(priority, 2)
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


def _pushes_since(conn: sqlite3.Connection, kinds: tuple[str, ...], since: int) -> int:
    marks = ",".join("?" * len(kinds))
    return int(conn.execute(f"SELECT COUNT(*) FROM push_log WHERE kind IN ({marks}) AND ts >= ?",
                            (*kinds, since)).fetchone()[0])


def _budget_threshold(conn: sqlite3.Connection, cfg: NtfyConfig,
                      sources: dict[str, SourceConfig] | None, now: int) -> float:
    """The score an alert needs to be pushed: the floor, or the week's (per_day × 7)-th
    highest alert score if that is higher, so that about per_day a day go out, the most
    serious, however many alerts there are."""
    from worldwatch.alerts.engine import alert_score

    scores = sorted((alert_score(json.loads(r["evidence"]), sources)[0] for r in conn.execute(
        "SELECT evidence FROM alerts WHERE opened_at >= ?", (now - 7 * 86400,))), reverse=True)
    k = cfg.per_day * 7
    return max(cfg.min_score, scores[k - 1] if len(scores) >= k else cfg.min_score)


async def notify_alerts(
    conn: sqlite3.Connection,
    alert_ids: list[int],
    client: httpx.AsyncClient | None = None,
    cfg: NtfyConfig | None = None,
    sources: dict[str, SourceConfig] | None = None,
    now: int | None = None,
) -> int:
    """Push the alerts that make the budget. Returns the count delivered. No-op
    (health-logged) when no channel is configured.

    The budget: an alert is pushed only if its score (alerts.engine.alert_score) is
    among the most serious, at least _budget_threshold (about cfg.per_day a day), and
    fewer than 2 × per_day went out in the last 24 h. Extreme ones (confirmed by two
    modalities or a stanza marked extreme, scoring at least extreme_score) are pushed
    at priority 5, at most extreme_per_week a week; the rest at 3 at most. The others
    are held: recorded, and on the dashboard, not pushed."""
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

    from worldwatch.alerts.engine import alert_score

    now = int(time.time()) if now is None else now
    owns_client = client is None
    client = client or httpx.AsyncClient()
    delivered = held = 0
    try:
        for alert in rows:
            score, _, eligible = alert_score(json.loads(alert["evidence"]), sources)
            extreme = (eligible and score >= cfg.extreme_score
                       and _pushes_since(conn, ("extreme",), now - 7 * 86400) < cfg.extreme_per_week)
            if not extreme and (score < _budget_threshold(conn, cfg, sources, now)
                                or _pushes_since(conn, ("alert", "extreme"), now - 86400) >= 2 * cfg.per_day):
                held += 1
                continue
            try:
                if await send_ntfy(client, cfg, alert, conn, sources, extreme=extreme):
                    delivered += 1
                    conn.execute("INSERT INTO push_log (ts, alert_id, kind) VALUES (?, ?, ?)",
                                 (now, alert["alert_id"], "extreme" if extreme else "alert"))
                    conn.commit()
            except httpx.HTTPError as e:
                record_health(conn, "notify", "push_error", f"alert={alert['alert_id']}: {e}")
    finally:
        if owns_client:
            await client.aclose()
    record_health(conn, "notify", "ok", f"delivered={delivered}" + (f" held={held}" if held else ""))
    return delivered
