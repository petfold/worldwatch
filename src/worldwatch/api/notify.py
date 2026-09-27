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
STAGE_PRIORITY = {0: 3, 1: 4, 2: 5}  # unconfirmed, confirmed, extreme (5 wakes)
STAGE_PREFIX = {0: "Unconfirmed", 1: "Confirmed", 2: "EXTREME"}


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
    home: tuple[tuple[float, float], ...] = ()  # the operator's places: only events reaching one may wake

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
            home=_parse_home(os.environ.get("WW_HOME")),
        )


def _parse_home(value: str | None) -> tuple[tuple[float, float], ...]:
    """WW_HOME: 'lat,lon' or several, 'lat,lon; lat,lon'."""
    out = []
    for part in (value or "").split(";"):
        if part.strip():
            lat, lon = (float(x) for x in part.split(","))
            out.append((lat, lon))
    return tuple(out)


def in_reach(evidence: list[dict], sources: dict[str, SourceConfig] | None,
             home: tuple[tuple[float, float], ...]) -> bool:
    """Whether the event can affect one of the operator's places: a member whose
    stanza gives a reach ([alerts] reach_km: km from its cell, or "global") that covers
    one. global_sensors: that many independent cells of the stream agreeing make it
    global (many radiation stations at once: a release, not a detector). Streams with
    no reach (attention, news, markets) never do. No home configured: every event."""
    import h3

    from worldwatch.alerts.engine import policy

    if not home:
        return True
    cells: dict[str, set[str]] = {}
    for e in evidence:
        cells.setdefault(e.get("stream_id", ""), set()).add(e.get("cell") or "")
    for sid, cs in cells.items():
        pol = policy((sources or {}).get(sid))
        reach = pol.get("reach_km")
        if reach is None:
            continue
        if reach == "global" or len(cs) >= int(pol.get("global_sensors", 10**9)):
            return True
        for c in cs:
            if h3.is_valid_cell(c) and any(
                    h3.great_circle_distance(h3.cell_to_latlng(c), p, unit="km") <= float(reach)
                    for p in home):
                return True
    return False


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
    prefix: str | None = None,
) -> tuple[str, str, int, list[str]]:
    """(title, message, priority, tags) for an alert row.

    Title: 'WW Confirmed: Radiation, Europe (EURDEP) + 1 - Finland' (ASCII: an HTTP
    header); prefix overrides the stage word. With `conn`, each evidence line also
    says what was observed (from the consolidated bin) and the usual level there —
    context for the reader only; the alert itself was opened on q_values alone."""
    evidence = json.loads(alert["evidence"])
    severity = float(alert["severity"])
    sources = sources or {}
    from worldwatch.alerts.engine import stage_of

    stage = max(int(alert["stage"]) if "stage" in alert.keys() else 0, stage_of(evidence, sources))
    is_silence = (bool(evidence) and all(e.get("q_value") is None for e in evidence)
                  and not any(e.get("kind") == "source_alert" for e in evidence))

    labels: list[str] = []
    for e in evidence:
        lab = context.display_for(e["stream_id"], sources.get(e["stream_id"])).label
        if lab not in labels:
            labels.append(lab)
    what = labels[0] + (f" + {len(labels) - 1}" if len(labels) > 1 else "") if labels else "alert"
    cells = [e["cell"] for e in evidence if e.get("cell")] or [alert["cell"]]
    where = context.place_names(cells) or context.where(alert["cell"])
    title = _ascii_text(f"WW {prefix or STAGE_PREFIX.get(stage, 'Alert')}: {what} - {where}")

    lines = [
        certainty(evidence, sources, stage) + f" (severity {severity:.2f})",
        f"Where: {context.place(cells[0])}",
    ]
    main = context.place_names(cells[:1])
    others = context.place_names([c for c in cells[1:] if context.place_names([c]) != main], limit=3)
    if others:
        lines.append(f"Also: {others}")
    starts = [e["bin_start"] for e in evidence if e.get("bin_start") is not None]
    if starts:
        lines.append(f"Latest data: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(max(starts)))}")
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
        lines.append("News in the area (context, not evidence):")
        for cfg, rec in news:
            url = evstore.link(cfg, rec)
            lines.append(f"  > {evstore.summary(cfg, rec)}" + (f" [{evstore.domain(url)}]" if url else ""))
    message = "\n".join(lines)
    while len(message.encode()) > MAX_MESSAGE_BYTES and len(lines) > 4:
        lines.pop()  # drop the last detail line
        message = "\n".join(lines + ["(truncated: the full report has everything)"])

    priority = 5 if severity >= 0.9 else 4 if severity >= 0.7 else 3
    tags = ["mute"] if is_silence else ["rotating_light"]
    return title, message, priority, tags


def certainty(evidence: list[dict], sources: dict[str, SourceConfig] | None, stage: int) -> str:
    """How sure the alert is, in words: its stage and why."""
    from worldwatch.alerts.engine import policy

    sources = sources or {}
    kinds = sorted({e.get("modality") for e in evidence})
    issued = [e for e in evidence if e.get("kind") == "source_alert"]
    streams = {e["stream_id"] for e in evidence}
    word = STAGE_PREFIX.get(stage, "Alert")
    if issued:
        label = context.display_for(issued[0]["stream_id"], sources.get(issued[0]["stream_id"])).label
        why = f"issued by {label}"
    elif len(kinds) >= 2:
        why = f"{len(kinds)} independent kinds of measurement agree"
    elif len(streams) == 1 and policy(sources.get(next(iter(streams)))).get("single_source"):
        n = len({e.get("cell") for e in evidence})
        why = f"{n} independent sensor{'s' if n != 1 else ''} of one network agree"
    elif evidence and all(e.get("q_value") is None for e in evidence):
        why = "sources that stopped reporting"
    else:
        why = "one kind of measurement so far"
    if stage == 2:
        why += ", and very improbable by chance"
    return f"{word}: {why}"


def _ascii_text(s: str) -> str:
    """Plain ASCII for a header: accents folded ('Cote d'Ivoire'), the rest dropped."""
    import unicodedata

    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


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
    """'- Radiation, Europe (EURDEP): unusually high (1 in 2,500): 0.327 µSv/h,
    usually 0.100 µSv/h @ Portugal (41.2N 8.2W)'"""
    cfg = sources.get(e["stream_id"])
    label = context.display_for(e["stream_id"], cfg).label
    at = f" @ {context.place(e['cell'])}" if e.get("cell") and context.cell_center(e["cell"]) else ""
    if e.get("kind") == "source_alert":
        return f"- {label}: issued by the source{at}"
    if e.get("q_value") is None:
        return f"- {label}: stopped reporting (presence {e['presence_q']:.2f}){at}"
    line = f"- {label}: {context.rarity_phrase(e['q_value'])}"
    if conn is not None and e.get("cell"):
        row = context.bin_row(conn, e["stream_id"], e["cell"], e.get("scale"), e.get("bin_start"))
        if row is not None:
            line += f": {context.describe_bin(e['stream_id'], cfg, row)}"
            usual = context.typical(conn, e["stream_id"], cfg, e["cell"], e.get("bin_start"))
            if usual:
                line += f", usually {usual}"
    return line + at


def _ascii(s: str) -> str:
    """HTTP header values must be latin-1; keep button labels plain ASCII."""
    return s.encode("ascii", "ignore").decode().replace(",", " ").replace(";", " ") or "Link"


async def send_ntfy(
    client: httpx.AsyncClient,
    cfg: NtfyConfig,
    alert: sqlite3.Row,
    conn: sqlite3.Connection | None = None,
    sources: dict[str, SourceConfig] | None = None,
    priority: int | None = None,
    prefix: str | None = None,
) -> bool:
    """Publish one alert to ntfy. Returns True on success. priority: overrides the
    formatted one (5 wakes the operator); prefix: goes before the title (ASCII)."""
    title, message, formatted, tags = format_alert(alert, conn, sources, prefix=prefix)
    priority = formatted if priority is None else priority
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
        # tapping the push opens the alert's full report; the map is a button
        headers["Click"] = f"{cfg.dashboard_url}/alert/{alert['alert_id']}"
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
    fewer than 2 × per_day went out in the last 24 h. An early (unconfirmed) alert,
    one very strong signal (alerts.engine.PROVISIONAL_SCORE), skips the score test but
    not the 24-h cap. The others are held: recorded, and on the dashboard, not pushed.

    The priority follows the alert's stage: unconfirmed (one modality) 3, confirmed
    (two or more, or a stanza marked extreme) 4, extreme (confirmed, scoring at least
    extreme_score) 5, which wakes, at most extreme_per_week a week (then 4). Extreme
    ones skip the budget. With cfg.home set, only events that can reach one of the
    operator's places (in_reach) wake or go above 3: an earthquake across the world is
    news, not an emergency. An alert already pushed is pushed again, skipping the budget,
    only when escalation has raised its stage: an early unconfirmed push, then an
    update at the higher priority as confirmation comes in."""
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
            evidence = json.loads(alert["evidence"])
            score, _, confirmed = alert_score(evidence, sources)
            stage = max(int(alert["stage"]), (2 if score >= cfg.extreme_score else 1) if confirmed else 0)
            pushed = conn.execute("SELECT MAX(stage) FROM push_log WHERE alert_id = ?",
                                  (alert["alert_id"],)).fetchone()[0]
            if pushed is not None and stage <= pushed:
                continue  # already pushed at this stage
            extreme = (stage == 2
                       and _pushes_since(conn, ("extreme",), now - 7 * 86400) < cfg.extreme_per_week)
            early = stage == 0 and any(e.get("kind") == "provisional" for e in evidence)
            if pushed is None and not extreme and (
                    (not early and score < _budget_threshold(conn, cfg, sources, now))
                    or _pushes_since(conn, ("alert", "extreme"), now - 86400) >= 2 * cfg.per_day):
                held += 1
                continue
            near = in_reach(evidence, sources, cfg.home)
            extreme = extreme and near  # waking is for what can reach you
            priority = 5 if extreme else min(STAGE_PRIORITY[stage], 4 if near else 3)
            prefix = STAGE_PREFIX[stage]
            if not near:
                prefix += ", far away"  # no distance: the push must not locate the operator
            if pushed is not None:
                prefix += " (update)"
            try:
                if await send_ntfy(client, cfg, alert, conn, sources, priority=priority, prefix=prefix):
                    delivered += 1
                    conn.execute("INSERT INTO push_log (ts, alert_id, kind, stage) VALUES (?, ?, ?, ?)",
                                 (now, alert["alert_id"], "extreme" if extreme else "alert", stage))
                    conn.commit()
            except httpx.HTTPError as e:
                record_health(conn, "notify", "push_error", f"alert={alert['alert_id']}: {e}")
    finally:
        if owns_client:
            await client.aclose()
    record_health(conn, "notify", "ok", f"delivered={delivered}" + (f" held={held}" if held else ""))
    return delivered
