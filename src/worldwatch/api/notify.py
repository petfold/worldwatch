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

from worldwatch.api import context
from worldwatch.config.loader import SourceConfig
from worldwatch.instrument import record_health

MAX_EVIDENCE_LINES = 8


@dataclass(frozen=True)
class NtfyConfig:
    server: str
    topic: str
    token: str | None = None

    @classmethod
    def from_env(cls) -> NtfyConfig | None:
        topic = os.environ.get("WW_NTFY_TOPIC")
        if not topic:
            return None
        return cls(
            server=os.environ.get("WW_NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
            topic=topic,
            token=os.environ.get("WW_NTFY_TOKEN"),
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
    is_silence = all(e.get("q_value") is None for e in evidence) and bool(evidence)
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
    if len(evidence) > MAX_EVIDENCE_LINES:
        lines.append(f"+{len(evidence) - MAX_EVIDENCE_LINES} more signals")
    lines.append("")
    lines.append(f"streams: {', '.join(streams[:6])}")

    priority = 5 if severity >= 0.9 else 4 if severity >= 0.7 else 3
    tags = ["rotating_light"] if not is_silence else ["mute"]
    return title, "\n".join(lines), priority, tags


def _evidence_line(
    e: dict, conn: sqlite3.Connection | None, sources: dict[str, SourceConfig]
) -> str:
    """'- Earthquakes (M1+) [physical] 1-in-2,500 high: 4 quakes, max M5.1 @ 35.9N 140.1E'"""
    cfg = sources.get(e["stream_id"])
    label = context.display_for(e["stream_id"], cfg).label
    head = f"- {label} [{e['modality']}]"
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
    if url := context.map_url(alert["cell"]):
        headers["Click"] = url  # tapping the push opens the region on a map
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
