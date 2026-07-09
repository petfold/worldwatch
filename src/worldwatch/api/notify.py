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
from dataclasses import dataclass

import httpx

from worldwatch.instrument import record_health


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


def format_alert(alert: sqlite3.Row) -> tuple[str, str, int, list[str]]:
    """(title, message, priority, tags) for an alert row."""
    evidence = json.loads(alert["evidence"])
    modalities = sorted({e["modality"] for e in evidence})
    streams = [e["stream_id"] for e in evidence]
    severity = float(alert["severity"])

    title = f"Worldwatch alert - {alert['cell']}"  # ASCII only (HTTP header)
    is_silence = all(e.get("q_value") is None for e in evidence) and bool(evidence)
    kind = "multi-source silence" if is_silence else "corroborated surprise"
    message = (
        f"{kind} (severity {severity:.2f})\n"
        f"region {alert['cell']} @ scale {alert['scale']}\n"
        f"{len(streams)} signals across {len(modalities)} modalities: "
        f"{', '.join(modalities)}\n"
        f"streams: {', '.join(streams[:6])}"
    )
    priority = 5 if severity >= 0.9 else 4 if severity >= 0.7 else 3
    tags = ["rotating_light"] if not is_silence else ["mute"]
    return title, message, priority, tags


async def send_ntfy(client: httpx.AsyncClient, cfg: NtfyConfig, alert: sqlite3.Row) -> bool:
    """Publish one alert to ntfy. Returns True on success."""
    title, message, priority, tags = format_alert(alert)
    headers = {
        "Title": title,
        "Priority": str(priority),
        "Tags": ",".join(tags),
    }
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
                if await send_ntfy(client, cfg, alert):
                    delivered += 1
            except httpx.HTTPError as e:
                record_health(conn, "notify", "push_error", f"alert={alert['alert_id']}: {e}")
    finally:
        if owns_client:
            await client.aclose()
    record_health(conn, "notify", "ok", f"delivered={delivered}")
    return delivered
