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
STAGE_PREFIX = {-1: "Below harm levels", 0: "Unconfirmed", 1: "Confirmed", 2: "EXTREME"}


def effective(conn: sqlite3.Connection | None, evidence: list[dict],
              sources: dict[str, SourceConfig] | None, extreme_score: float | None = None):
    """(stage, score, harm) of an alert as a person should see it: the evidence that
    counts (harm.assess drops signals below their harm floor), its stage from that,
    raised by a confirmed harm level. Stage -1: nothing left, below harm levels."""
    from worldwatch.alerts.engine import alert_score, extreme_score as env_extreme
    from worldwatch.api import harm as harm_mod

    h = harm_mod.assess(conn, evidence, sources)
    if not h.kept:
        return -1, 0.0, h
    score, _, confirmed = alert_score(h.kept, sources)
    limit = env_extreme() if extreme_score is None else extreme_score
    stage = (2 if score >= limit else 1) if confirmed else 0
    return max(stage, h.stage), score, h


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

    A phone shows a push as two short lines, so those say what happened and where.
    The title is the most specific thing known: a source's own words for the event
    ('M6.6 80 km ENE of Tadine, New Caledonia, depth 10 km'), else the signal and its
    harm or direction, then the place unless the words already name it (ASCII: an
    HTTP header). The message opens with the readings, observed and usual. How sure
    it is (the stage, 'WW Confirmed: ...'), the severity, the exact place and the data
    time come last; truncation takes detail lines first, never those. prefix
    overrides the stage words (the notifier adds 'far away', '(update)'). With
    `conn`, each reading comes from the consolidated bin with the usual level there:
    context for the reader only; the alert itself was opened on q_values alone."""
    evidence = json.loads(alert["evidence"])
    severity = float(alert["severity"])
    sources = sources or {}
    stage, _, hm = effective(conn, evidence, sources)
    is_silence = (bool(evidence) and all(e.get("q_value") is None for e in evidence)
                  and not any(e.get("kind") == "source_alert" for e in evidence))

    cells = [e["cell"] for e in evidence if e.get("cell")] or [alert["cell"]]
    where = context.place_names(cells) or context.where(alert["cell"])
    stories = {i: _stories(e, conn, sources) for i, e in enumerate(evidence[:MAX_EVIDENCE_LINES])}
    head = _headline_index(evidence, stories, hm)
    what = (_headline(evidence[head], stories.get(head, []), conn, sources, hm.members.get(head))
            if evidence else "Alert")
    title = _ascii_text(what if _names_place(what, where) else f"{what} - {where}")

    body: list[str] = []
    order = ([head] + [i for i in range(len(evidence)) if i != head]) if evidence else []
    titled = bool(evidence) and bool(stories.get(head))  # the title is that signal's first story
    for i in order[:MAX_EVIDENCE_LINES]:
        e, mh = evidence[i], hm.members.get(i)
        if not (i == head and titled and e.get("kind") == "source_alert"):  # "issued": in the footer
            body.append(_evidence_line(e, conn, sources) + (f" [{mh.label}]" if mh else ""))
        for rec in stories.get(i, [])[1 if i == head and titled else 0:]:
            cfg = sources.get(e["stream_id"])
            url = evstore.link(cfg, rec)
            body.append(f"  > {evstore.summary(cfg, rec)}" + (f" [{evstore.domain(url)}]" if url else ""))
    if len(evidence) > MAX_EVIDENCE_LINES:
        body.append(f"+{len(evidence) - MAX_EVIDENCE_LINES} more signals")
    news = _news_in_area(alert, conn, sources)
    if news:
        shown = {evstore.summary(sources.get(e["stream_id"]), r) for i, e in enumerate(evidence)
                 for r in stories.get(i, [])}
        news = [(cfg, rec) for cfg, rec in news if evstore.summary(cfg, rec) not in shown]
    if news:
        body += [""] * bool(body)
        for cfg, rec in news:  # context, not evidence (ADR 0001); the report page says so
            url = evstore.link(cfg, rec)
            kind = "News" if "news" in cfg.topic_tags else "Nearby"
            body.append(f"{kind}: {evstore.summary(cfg, rec)}" + (f" [{evstore.domain(url)}]" if url else ""))

    why = certainty(hm.kept or evidence, sources, stage).split(": ", 1)[-1]
    footer = [""] * bool(body) + [f"WW {prefix or STAGE_PREFIX.get(stage, 'Alert')}: {why} (severity {severity:.2f})",
              f"Where: {context.place(cells[0])}"]
    main = context.place_names(cells[:1])
    others = context.place_names([c for c in cells[1:] if context.place_names([c]) != main], limit=3)
    if others:
        footer.append(f"Also: {others}")
    starts = [e["bin_start"] for e in evidence if e.get("bin_start") is not None]
    if starts:
        footer.append(f"Latest data: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(max(starts)))}")
    message = "\n".join(body + footer)
    while len(message.encode()) > MAX_MESSAGE_BYTES and len(body) > 1:
        body.pop()  # the last detail line; the stage and place stay
        message = "\n".join(body + ["(truncated: the full report has everything)"] + footer)

    priority = 5 if severity >= 0.9 else 4 if severity >= 0.7 else 3
    tags = ["mute"] if is_silence else ["rotating_light"]
    return title, message, priority, tags


def _headline_index(evidence: list[dict], stories: dict[int, list], hm) -> int:
    """The signal the push leads with: one the source itself issued, else one with a
    story (the source's words for what happened), else one at a harm level, else the first."""
    for want in (lambda i, e: e.get("kind") == "source_alert" and stories.get(i),
                 lambda i, e: stories.get(i),
                 lambda i, e: (mh := hm.members.get(i)) is not None and mh.level > 0):
        for i, e in enumerate(evidence):
            if want(i, e):
                return i
    return 0


def _headline(e: dict, stories: list, conn: sqlite3.Connection | None,
              sources: dict[str, SourceConfig], mh) -> str:
    """What happened, as specifically as the signal allows: the source's words, its
    harm level, or its direction and reading. The label without its source in
    brackets ('Radiation, Europe'): the message names the source."""
    import re

    cfg = sources.get(e["stream_id"])
    label = re.sub(r"\s*\([^)]*\)$", "", context.display_for(e["stream_id"], cfg).label)
    if stories:
        return evstore.summary(cfg, stories[0])
    if mh is not None and mh.level > 0:
        return f"{label}: {mh.label}"
    if e.get("kind") == "source_alert":
        return f"{label}: issued by the source"
    if e.get("q_value") is None:
        return f"{label} stopped reporting"
    what = f"{label} unusually {context.rarity(e['q_value'])[1]}"
    row = (context.bin_row(conn, e["stream_id"], e["cell"], e.get("scale"), e.get("bin_start"))
           if conn is not None and e.get("cell") else None)
    return f"{what}: {context.describe_bin(e['stream_id'], cfg, row)}" if row is not None else what


def _names_place(what: str, where: str) -> bool:
    """Whether the headline already names the place ('..., New Caledonia')."""
    low = what.lower()
    return any(len(part) > 3 and part in low
               for part in (p.strip().lower().removeprefix("off ") for p in where.replace("+", ",").split(",")))


def certainty(evidence: list[dict], sources: dict[str, SourceConfig] | None, stage: int) -> str:
    """How sure the alert is, in words: its stage and why."""
    from worldwatch.alerts.engine import policy

    sources = sources or {}
    kinds = sorted({e.get("modality") for e in evidence})
    issued = [e for e in evidence if e.get("kind") == "source_alert"]
    streams = {e["stream_id"] for e in evidence}
    word = STAGE_PREFIX.get(stage, "Alert")
    if stage == -1:
        return f"{word}: unusual, but every reading is below the level where it could do harm"
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

    s = s.replace("µ", "u")  # µSv/h, µg/m³: the symbols a reading may carry (², ³ fold to digits)
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


def source_links(
    alert: sqlite3.Row, conn: sqlite3.Connection | None, sources: dict[str, SourceConfig] | None
) -> list[tuple[str, str]]:
    """(label, url) for the pages a person would open next, most specific first.
    Signal by signal, the push's lead signal first: its own event pages (the
    evidence store's links: the quake, the warning), then the pages its stanza
    names for people ([display] links: a radiation map at the place, the source's
    own view), then the place on OpenStreetMap. Duplicates dropped."""
    sources = sources or {}
    evidence = json.loads(alert["evidence"])
    out: list[tuple[str, str]] = []

    def add(label: str, url: str | None) -> None:
        if url and all(url != u for _, u in out):
            out.append((label, url))

    stories = {i: _stories(e, conn, sources) for i, e in enumerate(evidence)}
    head = _headline_index(evidence, stories, effective(conn, evidence, sources)[2]) if evidence else 0
    for i in ([head] + [i for i in range(len(evidence)) if i != head]) if evidence else []:
        e, cfg = evidence[i], sources.get(evidence[i]["stream_id"])
        for rec in stories[i]:
            url = evstore.link(cfg, rec)
            add(evstore.domain(url)[:24] if url else "", url)
        cell = e.get("cell") or alert["cell"]
        for label, template in context.display_for(e["stream_id"], cfg).links:
            add(label, context.fill_link(template, cell, e.get("bin_start"), stories[i][0] if stories[i] else None))
    add("Map", context.map_url(alert["cell"]))
    return out


def _evidence_line(
    e: dict, conn: sqlite3.Connection | None, sources: dict[str, SourceConfig]
) -> str:
    """'0.327 µSv/h, usually 0.100 µSv/h: Radiation, Europe (EURDEP), unusually high
    (1 in 2,500) @ Portugal (41.2N 8.2W)': the reading first, as a phone shows it."""
    cfg = sources.get(e["stream_id"])
    label = context.display_for(e["stream_id"], cfg).label
    at = f" @ {context.place(e['cell'])}" if e.get("cell") and context.cell_center(e["cell"]) else ""
    if e.get("kind") == "source_alert":
        return f"{label}: issued by the source{at}"
    if e.get("q_value") is None:
        return f"{label}: stopped reporting (presence {e['presence_q']:.2f}){at}"
    rarity = context.rarity_phrase(e["q_value"])
    if conn is not None and e.get("cell"):
        row = context.bin_row(conn, e["stream_id"], e["cell"], e.get("scale"), e.get("bin_start"))
        if row is not None:
            reading = context.describe_bin(e["stream_id"], cfg, row)
            usual = context.typical(conn, e["stream_id"], cfg, e["cell"], e.get("bin_start"))
            return f"{reading}" + (f", usually {usual}" if usual else "") + f": {label}, {rarity}{at}"
    return f"{label}: {rarity}{at}"


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
    buttons = source_links(alert, conn, sources)  # tap straight to the sources' own pages
    if cfg.dashboard_url:
        headers["Click"] = f"{cfg.dashboard_url}/alert/{alert['alert_id']}"  # the full report
    elif map_url:
        headers["Click"] = map_url  # no dashboard configured: open the region on a map
    verdicts: list[str] = []
    if cfg.dashboard_url and conn is not None:  # Useful / Not useful, from the phone
        from worldwatch.api import feedback

        verdicts = feedback.actions(conn, cfg.dashboard_url, int(alert["alert_id"]))
    # the first source's own page, then the verdicts (ntfy shows three buttons; the
    # map and further sources are a tap away on the dashboard's alert page)
    actions = [f"view, {_ascii(label)}, {url}" for label, url in buttons[:MAX_ACTIONS - len(verdicts)]]
    actions += verdicts
    if actions:
        headers["Actions"] = "; ".join(actions)
    if cfg.token:
        headers["Authorization"] = f"Bearer {cfg.token}"
    resp = await client.post(
        f"{cfg.server}/{cfg.topic}", content=message.encode(), headers=headers, timeout=15.0
    )
    resp.raise_for_status()
    return True


async def send_ntfy_note(
    client: httpx.AsyncClient, cfg: NtfyConfig, title: str, message: str, priority: int = 3,
) -> bool:
    """Publish a plain note about the system itself (not an alert): resource warnings."""
    if cfg.silent():
        priority = min(priority, 2)
    headers = {"Title": _ascii(title), "Priority": str(priority), "Tags": "gear"}
    if cfg.dashboard_url:
        headers["Click"] = f"{cfg.dashboard_url}/api/resources"
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
    scores = sorted((sc for r in conn.execute("SELECT evidence FROM alerts WHERE opened_at >= ?",
                                             (now - 7 * 86400,))
                     for st, sc, _ in [effective(conn, json.loads(r["evidence"]), sources, cfg.extreme_score)]
                     if st >= 0), reverse=True)  # harmless alerts are not competition
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
    fewer than 2 × per_day went out in the last 24 h. Harm (api.harm) comes first:
    signals below their harm floor do not count (an alert of nothing else is not
    pushed), and a confirmed harm level raises the stage and skips the budget. An early (unconfirmed) alert,
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

    now = int(time.time()) if now is None else now
    owns_client = client is None
    client = client or httpx.AsyncClient()
    delivered = held = harmless = 0
    threshold = None  # the budget's, computed once, if needed
    try:
        for alert in rows:
            stage, score, hm = effective(conn, json.loads(alert["evidence"]), sources, cfg.extreme_score)
            if stage < 0:
                harmless += 1  # interesting, on the dashboard; not for alerting
                continue
            evidence = hm.kept
            urgent = hm.stage >= 1  # a confirmed harm level: past the budget
            pushed = conn.execute("SELECT MAX(stage) FROM push_log WHERE alert_id = ?",
                                  (alert["alert_id"],)).fetchone()[0]
            if pushed is not None and stage <= pushed:
                continue  # already pushed at this stage
            extreme = (stage == 2
                       and _pushes_since(conn, ("extreme",), now - 7 * 86400) < cfg.extreme_per_week)
            early = stage == 0 and any(e.get("kind") == "provisional" for e in evidence)
            if pushed is None and not extreme and not urgent:
                if threshold is None and not early:
                    threshold = _budget_threshold(conn, cfg, sources, now)
                if ((not early and score < threshold)
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
    record_health(conn, "notify", "ok", f"delivered={delivered}" + (f" held={held}" if held else "")
                  + (f" harmless={harmless}" if harmless else ""))
    return delivered
