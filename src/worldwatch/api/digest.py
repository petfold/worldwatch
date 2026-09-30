"""The weekly report: every significant deviation, alerted or not, read by an LLM.

Alerts are for harm (ADR 0004), so much of what is unusual is never pushed: a
drop in a pollutant (a factory stopped?), a quiet internet in a country, a
deviation below harm levels, one detector. The week's digest keeps all of it:
per stream, how many readings and how many were beyond 1 in 1,000 against how
many chance alone gives; each deviating place, which way, how far, what was
observed against the usual level; the week's alerts; silences; feed health.

An LLM (the Anthropic API, WW_ANTHROPIC_API_KEY) reads the digest and writes an
analysis: what stands out, plausible explanations, coincidences worth a look,
likely artifacts, what to check. Both are stored (`digests`, a few tens of KB a
week), served at /digest, and announced by one silent push. Without a key the
digest alone is stored. Nothing personal goes out: public-source facts only,
never the operator's places.
"""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import time
from collections import defaultdict

import httpx

from worldwatch.api import context
from worldwatch.config.loader import SourceConfig
from worldwatch.instrument import record_health

WEEK = 7 * 86400
P_NOTABLE = 1e-3  # a reading this far into a tail (two-sided) is listed
PER_STREAM = 25  # places listed per stream, the most extreme first
DEFAULT_MODEL = "claude-opus-5-5"
API_URL = "https://api.anthropic.com/v1/messages"

SYSTEM = """You are the analyst of Worldwatch, a system that watches free public data \
streams (earthquakes, radiation, internet reachability and traffic, weather warnings, \
disasters, markets, attention, night lights) and flags readings that are unusual for \
each sensor's own learned normal (its level, trend and daily and weekly rhythm).

You get one week's digest: every significant deviation in both directions, including \
the many never alerted because they were in the harmless direction or below harm \
levels; the week's alerts; silences; feed health. Each stream says how many deviations \
chance alone would give, so an excess means something and a count near chance does not.

Write a short analysis for the operator:
1. What stands out, with plausible real-world explanations (a drop in a pollution or \
traffic signal can mean activity or production stopped; brighter nights can mean fires; \
more Wikipedia reading can point at news).
2. Coincidences across sources and places worth a look.
3. What is likely an artifact: Worldwatch's own vantage point (its probes run from one \
server), a feed glitch, a single detector, chance.
4. What to check next.
Be concrete: cite sources, places and dates from the digest, and say how sure you are. \
Do not invent events. If you know of real events at those times and places, you may \
mention them, marked as background knowledge. Plain markdown with headings and bullets, \
no tables, under 800 words."""


def _t(ts: int | float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))


def build_digest(conn: sqlite3.Connection, sources: dict[str, SourceConfig], week_end: int) -> str:
    """The week before week_end, as markdown facts."""
    from worldwatch.alerts.engine import tail_of
    from worldwatch.api import harm
    from worldwatch.api.notify import STAGE_PREFIX, effective, format_alert

    t0 = week_end - WEEK
    out = [f"# Worldwatch weekly digest, {_t(t0)} to {_t(week_end)} UTC", ""]

    # --- deviations, per stream and place
    readings: dict[str, int] = defaultdict(int)
    dev: dict[tuple[str, str, str], dict] = {}
    for r in conn.execute(
            "SELECT stream_id, cell, scale, bin_start, COALESCE(q_detect, q_value) AS q FROM surprise "
            "WHERE bin_start >= ? AND bin_start < ? AND q_value IS NOT NULL", (t0, week_end)):
        readings[r["stream_id"]] += 1
        q = float(r["q"])
        p = 2 * min(q, 1 - q)
        if p > P_NOTABLE:
            continue
        key = (r["stream_id"], r["cell"], "high" if q > 0.5 else "low")
        d = dev.setdefault(key, {"n": 0, "first": r["bin_start"], "last": r["bin_start"], "p": 1.0,
                                 "peak": None, "scales": set()})
        d["n"] += 1
        d["first"], d["last"] = min(d["first"], r["bin_start"]), max(d["last"], r["bin_start"])
        d["scales"].add(r["scale"])
        if p < d["p"]:
            d["p"], d["peak"] = p, dict(r)
    by_stream: dict[str, list] = defaultdict(list)
    for (sid, cell, way), d in dev.items():
        by_stream[sid].append((cell, way, d))

    out += ["## Deviations beyond 1 in 1,000, by stream", "",
            "Readings: how many were scored this week. Chance: how many would be this extreme "
            "by chance alone. Harmless way: the direction this stream never alerts on.", ""]
    for sid in sorted(by_stream, key=lambda s: -len(by_stream[s]) / max(readings[s] * P_NOTABLE, 1e-9)):
        cfg = sources.get(sid)
        disp = context.display_for(sid, cfg)
        tail = tail_of(cfg)
        n_dev = sum(d["n"] for _, _, d in by_stream[sid])
        out.append(f"### {disp.label}")
        if disp.about:
            out.append(disp.about)
        out.append(f"Readings {readings[sid]:,}; beyond 1 in 1,000: {n_dev:,} (chance: "
                   f"{readings[sid] * P_NOTABLE:,.1f}); {len(by_stream[sid])} place-directions."
                   + (f" Alerts only on {'rises' if tail == 'upper' else 'drops'}." if tail != "both" else ""))
        rows = sorted(by_stream[sid], key=lambda x: (x[2]["p"], -x[2]["n"]))
        for cell, way, d in rows[:PER_STREAM]:
            pk = d["peak"]
            line = (f"- {context.place(cell)}: {way}, strongest {context.rarity(pk['q'])[0].replace('1-in-', '1 in ')} "
                    f"at {_t(pk['bin_start'])}, "
                    + ("once" if d["n"] == 1 else f"{d['n']} readings {_t(d['first'])} to {_t(d['last'])}"))
            if tail != "both" and (way == "high") != (tail == "upper"):
                line += " [harmless way]"
            row = context.bin_row(conn, sid, cell, pk["scale"], pk["bin_start"])
            if row is not None:
                line += f"; observed {context.describe_bin(sid, cfg, row)}"
                usual = context.typical(conn, sid, cfg, cell, pk["bin_start"])
                if usual:
                    line += f", usually {usual}"
            mh = harm.member_harm(conn, {"stream_id": sid, "cell": cell, "scale": pk["scale"],
                                         "bin_start": pk["bin_start"], "q_value": pk["q"]}, cfg)
            if mh is not None:
                line += f"; harm: {mh.label}"
            out.append(line)
        if len(rows) > PER_STREAM:
            out.append(f"- ... and {len(rows) - PER_STREAM} more places")
        out.append("")
    quiet = sorted(s for s in readings if s not in by_stream)
    if quiet:
        out += ["Streams with no deviation beyond 1 in 1,000: " + ", ".join(
            context.display_for(s, sources.get(s)).label for s in quiet), ""]

    # --- alerts
    alerts = conn.execute("SELECT * FROM alerts WHERE opened_at >= ? AND opened_at < ? ORDER BY opened_at",
                          (t0, week_end)).fetchall()
    pushed = {r[0]: r[1] for r in conn.execute(
        "SELECT alert_id, MAX(stage) FROM push_log WHERE ts >= ? GROUP BY alert_id", (t0,))}
    out += [f"## Alerts: {len(alerts)} opened, {sum(a['alert_id'] in pushed for a in alerts)} pushed", ""]
    by_stage: dict[int, list] = defaultdict(list)
    for a in alerts:
        stage = effective(conn, json.loads(a["evidence"]), sources)[0]
        by_stage[stage].append(a)
    for stage in sorted(by_stage, reverse=True):
        group = by_stage[stage]
        out.append(f"### {STAGE_PREFIX.get(stage, stage)}: {len(group)}")
        for a in group[-30:]:
            title = format_alert(a, conn, sources)[0]  # what and where; the stage is the group
            out.append(f"- {_t(a['opened_at'])} #{a['alert_id']} {title}"
                       + (" (pushed)" if a["alert_id"] in pushed else "")
                       + (f" (labelled {a['label']})" if a["label"] else ""))
        if len(group) > 30:
            out.append(f"- ... and {len(group) - 30} earlier")
        out.append("")

    # --- silences and feed health
    silent = conn.execute(
        "SELECT stream_id, COUNT(*) AS n, COUNT(DISTINCT cell) AS cells FROM surprise "
        "WHERE bin_start >= ? AND bin_start < ? AND q_value IS NULL AND presence_q >= 0.99 "
        "GROUP BY stream_id ORDER BY n DESC", (t0, week_end)).fetchall()
    if silent:
        out += ["## Silences (a source not reporting when it should)", ""]
        out += [f"- {context.display_for(r['stream_id'], sources.get(r['stream_id'])).label}: "
                f"{r['n']} silent bin(s) in {r['cells']} place(s)" for r in silent]
        out.append("")
    health = conn.execute(
        "SELECT component, event, COUNT(*) AS n FROM health WHERE ts >= ? AND ts < ? "
        "AND event NOT IN ('ok', 'not_modified') GROUP BY component, event ORDER BY n DESC LIMIT 40",
        (t0, week_end)).fetchall()
    if health:
        out += ["## Feed and system health (events other than ok)", ""]
        out += [f"- {r['component']}: {r['event']} x{r['n']}" for r in health]
        out.append("")
    return "\n".join(out)


def analyze(digest: str, client: httpx.Client | None = None) -> tuple[str | None, str | None]:
    """(analysis, model) from the LLM, or (None, None) without an API key."""
    key = os.environ.get("WW_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None, None
    model = os.environ.get("WW_DIGEST_MODEL", DEFAULT_MODEL)
    owns = client is None
    client = client or httpx.Client(timeout=300.0)
    try:
        r = client.post(API_URL, headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"},
                        json={"model": model, "max_tokens": 4000, "system": SYSTEM,
                              "messages": [{"role": "user", "content": digest}]})
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        return text.strip() or None, model
    finally:
        if owns:
            client.close()


def run_digest(conn: sqlite3.Connection, sources: dict[str, SourceConfig], now: int | None = None,
               client: httpx.Client | None = None) -> int | None:
    """Build, analyse, store and announce the week's report, once per week (the week
    ends at the last midnight UTC). Returns its week_end, or None if already done."""
    now = int(time.time()) if now is None else now
    week_end = now - now % 86400
    if conn.execute("SELECT 1 FROM digests WHERE week_end = ?", (week_end,)).fetchone():
        return None
    digest = build_digest(conn, sources, week_end)
    analysis = model = None
    try:
        analysis, model = analyze(digest, client)
    except (httpx.HTTPError, ValueError) as e:
        record_health(conn, "digest", "llm_error", str(e)[:300], ts=now)
    conn.execute("INSERT INTO digests (week_end, created_at, digest, analysis, model) VALUES (?, ?, ?, ?, ?)",
                 (week_end, now, digest, analysis, model))
    conn.commit()
    _announce(conn, week_end, digest, analysis, client)
    record_health(conn, "digest", "ok", f"week_end={week_end} bytes={len(digest)}"
                  + (f" analysis={len(analysis)}" if analysis else " no_analysis"), ts=now)
    return week_end


def _announce(conn: sqlite3.Connection, week_end: int, digest: str, analysis: str | None,
              client: httpx.Client | None) -> None:
    """One silent push (priority 2): the week's report is ready."""
    from worldwatch.api.notify import NtfyConfig, _ascii_text

    cfg = NtfyConfig.from_env()
    if cfg is None:
        return
    first = next((ln.lstrip("#- ").strip() for ln in (analysis or "").splitlines()
                  if ln.strip() and not ln.startswith("#")), "")
    body = (first[:600] + "\n\n" if first else "") + "The full report: every significant deviation of the week."
    headers = {"Title": _ascii_text(f"WW Weekly report, week to {time.strftime('%Y-%m-%d', time.gmtime(week_end))}"),
               "Priority": "2", "Tags": "memo"}
    if cfg.dashboard_url:
        headers["Click"] = f"{cfg.dashboard_url}/digest/{week_end}"
    if cfg.token:
        headers["Authorization"] = f"Bearer {cfg.token}"
    owns = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        client.post(f"{cfg.server}/{cfg.topic}", content=body.encode(), headers=headers).raise_for_status()
    except httpx.HTTPError as e:
        record_health(conn, "digest", "push_error", str(e)[:300])
    finally:
        if owns:
            client.close()


# --- the page


def render_digest(conn: sqlite3.Connection, week_end: int | None = None) -> str | None:
    row = (conn.execute("SELECT * FROM digests WHERE week_end = ?", (week_end,)).fetchone() if week_end
           else conn.execute("SELECT * FROM digests ORDER BY week_end DESC LIMIT 1").fetchone())
    if row is None:
        return None
    weeks = [r[0] for r in conn.execute("SELECT week_end FROM digests ORDER BY week_end DESC LIMIT 12")]
    nav = " · ".join(f'<a href="/digest/{w}">{time.strftime("%Y-%m-%d", time.gmtime(w))}</a>' for w in weeks)
    analysis = (markdown(row["analysis"]) + f'<p class=muted>Analysis by {html.escape(row["model"] or "?")}: '
                "an LLM reading the facts below; check before acting on it.</p>"
                if row["analysis"] else "<p class=muted>No analysis (no LLM key configured, or it failed).</p>")
    return PAGE.format(title=f"WW weekly report, week to {time.strftime('%Y-%m-%d', time.gmtime(row['week_end']))}",
                       nav=nav, analysis=analysis, digest=markdown(row["digest"]))


def markdown(text: str) -> str:
    """The little markdown the digest and the analysis use (headings, bullets, numbered
    lists, bold, code, links), escaped first: nothing in it can run."""
    out, lst = [], None

    def inline(s: str) -> str:
        s = html.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
        return re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2" rel="noopener noreferrer">\1</a>', s)

    for ln in text.splitlines():
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        item = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.*)", ln)
        kind = "ol" if re.match(r"^\s*\d+\.\s", ln) else "ul"
        if lst and not item:
            out.append(f"</{lst}>")
            lst = None
        if m:
            n = len(m.group(1)) + 1
            out.append(f"<h{n}>{inline(m.group(2))}</h{n}>")
        elif item:
            if lst != kind:
                if lst:
                    out.append(f"</{lst}>")
                out.append(f"<{kind}>")
                lst = kind
            out.append(f"<li>{inline(item.group(1))}</li>")
        elif ln.strip():
            out.append(f"<p>{inline(ln)}</p>")
    if lst:
        out.append(f"</{lst}>")
    return "\n".join(out)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ --bg: #0b0f14; --panel: #11161d; --line: #223040; --text: #dfe6ee; --muted: #8a99a8; --link: #7cc4ff; }}
  body {{ margin: 0 auto; max-width: 820px; padding: 16px; font: 15px/1.5 system-ui, sans-serif;
         background: var(--bg); color: var(--text); }}
  h1 {{ font-size: 20px; }} h2 {{ font-size: 17px; margin-top: 26px; }} h3 {{ font-size: 15px; margin: 18px 0 4px; }}
  h4, h5 {{ font-size: 14px; }} a {{ color: var(--link); }} .muted {{ color: var(--muted); }}
  li {{ margin: 3px 0; overflow-wrap: anywhere; }} p {{ margin: 6px 0; }}
  details {{ background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 8px 12px; margin-top: 20px; }}
  summary {{ cursor: pointer; font-weight: 600; }}
</style></head><body>
<h1>{title}</h1>
<div class=muted>{nav}</div>
<h2>Analysis</h2>
{analysis}
<details open><summary>The facts: every significant deviation of the week</summary>
{digest}
</details>
</body></html>
"""
