"""One alert's full report, as a web page (presentation only).

Tapping a push opens /alert/<id>: what happened and where, how sure the alert is
and why (its stage and the confirmation history), how improbable, whether it can
reach the operator's places, every signal with what was observed against the usual
level, the source pages behind it, and the news in the area. Server-rendered so it
works on a phone without the map; every string from outside is escaped.
"""

from __future__ import annotations

import html
import json
import os
import sqlite3
import time

from worldwatch.api import context
from worldwatch.config.loader import SourceConfig
from worldwatch import evidence as evstore

SPARK_SECONDS = 3 * 86400  # the history drawn behind each signal
MODALITY_WORDS = {
    "physical": "physical sensors",
    "infrastructural": "internet and power measurements",
    "economic": "markets",
    "informational": "attention and news",
}


def _e(v: object) -> str:
    return html.escape(str(v), quote=True)


def _t(ts: int | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts)) if ts else ""


def _link(url: str | None, text: str) -> str:
    if not url or not str(url).startswith(("https://", "http://")):
        return _e(text)
    return f'<a href="{_e(url)}" target="_blank" rel="noopener noreferrer">{_e(text)}</a>'


def render_report(conn: sqlite3.Connection, alert_id: int,
                  sources: dict[str, SourceConfig] | None) -> str | None:
    from worldwatch.alerts.engine import alert_score, stage_of
    from worldwatch.api.notify import (STAGE_PREFIX, _news_in_area, _parse_home, _stories,
                                       certainty, effective, format_alert, in_reach)

    row = conn.execute("SELECT * FROM alerts WHERE alert_id = ?", (alert_id,)).fetchone()
    if row is None:
        return None
    sources = sources or {}
    ev = json.loads(row["evidence"])
    stage, _, hm = effective(conn, ev, sources)
    title, _, _, _ = format_alert(row, conn, sources)
    score, kinds, _ = alert_score(hm.kept or ev, sources)
    home = _parse_home(os.environ.get("WW_HOME"))
    pushes = conn.execute("SELECT ts, kind, stage FROM push_log WHERE alert_id = ? ORDER BY ts",
                          (alert_id,)).fetchall()
    cells = [e["cell"] for e in ev if e.get("cell")] or [row["cell"]]
    main = context.place_names(cells[:1])
    also = context.place_names([c for c in cells[1:] if context.place_names([c]) != main], limit=6)

    # --- summary
    sev = float(row["severity"])
    summary = [
        ("What", _e(", ".join(dict.fromkeys(
            context.display_for(e["stream_id"], sources.get(e["stream_id"])).label for e in ev)))),
        ("Where", _e(context.place(cells[0])) + (f"<br><span class=muted>also {_e(also)}</span>" if also else "")
         + f"<br><span class=muted>region {_e(row['cell'])}</span>"),
        ("When", f"opened {_e(_t(row['opened_at']))}" + (
            f"<br>last escalated {_e(_t(row['escalated_at']))}" if row["escalated_at"] else "")),
        ("Certainty", f"<b class=stage{stage}>{_e(STAGE_PREFIX.get(stage, '?'))}</b> — "
                      f"{_e(certainty(hm.kept or ev, sources, stage).split(': ', 1)[-1])}<br><span class=muted>"
                      f"{_e(_next_step(stage))}</span>"),
        ("Surprise", f"score {score:.1f} <span class=muted>— the sum over independent signals of "
                     f"−log10 p (p: the chance of a reading this extreme in normal times), × the "
                     f"number of kinds of measurement when two or more ({kinds} here)</span>"),
        ("Severity", f"{sev:.2f} <span class=muted>(0–1: how far into the tails the signals are and "
                     f"how many kinds agree; not a measure of harm)</span>"),
        ("Harm", _harm_text(hm)),
        ("Reach", _e(("can reach your places" if in_reach(hm.kept or ev, sources, home) else
                      "far from your places: it will not wake you") if home else
                     "no home set (WW_HOME): any extreme alert may wake")),
        ("Status", _e(row["status"]) + (f", labelled {_e(row['label'])}" if row["label"] else "")),
    ]

    # --- history: opened, confirmations, pushes
    history = [(int(row["opened_at"]), f"Opened as <b>{_e(STAGE_PREFIX.get(stage_of([e for e in ev if 'added_at' not in e], sources), '?'))}</b> "
                                       f"with {sum('added_at' not in e for e in ev)} signal(s)")]
    for ts in sorted({int(e["added_at"]) for e in ev if "added_at" in e}):
        added = [e for e in ev if e.get("added_at") == ts]
        names = ", ".join(dict.fromkeys(
            context.display_for(e["stream_id"], sources.get(e["stream_id"])).label for e in added))
        history.append((ts, f"Confirmation: {_e(names)} → <b>{_e(STAGE_PREFIX.get(int(added[0].get('stage', 0)), '?'))}</b>"))
    for p in pushes:
        history.append((int(p["ts"]), f"Pushed as {_e(STAGE_PREFIX.get(int(p['stage']), '?'))}"
                                      + (" (may wake)" if p["kind"] == "extreme" else " (no wake)")))
    history.sort()

    # --- signals
    cards = []
    for i, e in enumerate(ev):
        cfg = sources.get(e["stream_id"])
        disp = context.display_for(e["stream_id"], cfg)
        facts = []
        mh = hm.members.get(i)
        if e.get("kind") == "source_alert":
            facts.append("issued by the source")
        elif e.get("q_value") is None:
            facts.append(f"stopped reporting (presence {e['presence_q']:.2f})")
        else:
            facts.append(f"<b>{_e(context.rarity_phrase(e['q_value']))}</b>")
            b = context.bin_row(conn, e["stream_id"], e["cell"], e.get("scale"), e.get("bin_start")) if e.get("cell") else None
            if b is not None:
                facts.append("observed " + _e(context.describe_bin(e["stream_id"], cfg, b)))
                usual = context.typical(conn, e["stream_id"], cfg, e["cell"], e.get("bin_start"))
                if usual:
                    facts.append(f"usually {_e(usual)}")
            if mh is not None:
                facts.append(f"harm: <b>{_e(mh.label)}</b>" + (" <span class=muted>(does not count for alerting)</span>"
                                                               if e in hm.dropped else ""))
            facts.append(f"<span class=muted>q {e['q_value']:.6g}"
                         + (f", accumulated evidence {e['evidence']}" if e.get("evidence") is not None else "")
                         + "</span>")
        stories = "".join(
            f"<li>{_link(evstore.link(cfg, rec), evstore.summary(cfg, rec))}</li>" for rec in _stories(e, conn, sources))
        cards.append(f"""<div class=card>
  <div class=h>{_e(disp.label)} <span class=muted>· {_e(MODALITY_WORDS.get(e.get('modality'), e.get('modality')))}</span></div>
  <div class=muted>{_e(disp.about)}</div>
  <div>{' · '.join(facts)}</div>
  <div class=muted>{_e(context.place(e['cell'])) if e.get('cell') else ''}{' · data ' + _e(_t(e.get('bin_start'))) if e.get('bin_start') else ''}{' · added ' + _e(_t(e['added_at'])) if e.get('added_at') else ''}</div>
  {_spark(conn, e, cfg)}
  {f'<ul>{stories}</ul>' if stories else ''}
</div>""")

    news = "".join(f"<li>{_link(evstore.link(cfg, rec), evstore.summary(cfg, rec))}</li>"
                   for cfg, rec in _news_in_area(row, conn, sources))
    map_url = context.map_url(row["cell"])
    return PAGE.format(
        title=_e(title),
        summary="".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in summary),
        history="".join(f"<li><span class=muted>{_e(_t(ts))}</span> {text}</li>" for ts, text in history),
        cards="".join(cards),
        news=f"<h2>News in the area <span class=muted>(context, not evidence)</span></h2><ul>{news}</ul>" if news else "",
        links=" · ".join(x for x in (f'<a href="/?alert={alert_id}">on the Worldwatch map</a>',
                                     _link(map_url, "OpenStreetMap") if map_url else "") if x),
        alert_id=alert_id,
    )


def _harm_text(hm) -> str:
    if not hm.members:
        return "<span class=muted>no harm levels for these sources: judged on surprise alone</span>"
    out = _e(hm.label)
    if hm.peak > hm.level:
        out += f"<br><span class=muted>one reading: {_e(hm.peak_label)} (not confirmed by other sensors)</span>"
    if hm.dropped:
        out += (f"<br><span class=muted>{len(hm.dropped)} signal(s) below harm levels: unusual, "
                f"but not counted for alerting</span>")
    return out


def _next_step(stage: int) -> str:
    return {-1: "Every reading is below the level where it could do harm: kept for the record, never pushed.",
            0: "Becomes Confirmed when an independent kind of measurement agrees in the same region.",
            1: "Becomes Extreme if the confirmed evidence grows very improbable by chance.",
            2: "The highest stage: only these may wake you, and only if they can reach your places."}.get(stage, "")


def _spark(conn: sqlite3.Connection, e: dict, cfg: SourceConfig | None) -> str:
    """The signal's last few days as a line (continuous) or rate bars (counts), the
    alert's bin marked."""
    if not e.get("cell") or e.get("bin_start") is None or cfg is None:
        return ""
    t1 = int(e["bin_start"]) + 3600
    rows = conn.execute(
        "SELECT bin_start, scale, n, vmean FROM bins WHERE stream_id = ? AND cell = ? AND bin_start >= ? "
        "AND bin_start < ? ORDER BY bin_start", (e["stream_id"], e["cell"], t1 - SPARK_SECONDS, t1)).fetchall()
    from worldwatch.cascade.bins import bin_width

    disp = context.display_for(e["stream_id"], cfg)
    pts = []
    for r in rows:
        w = bin_width(int(r["scale"]))
        if cfg.flavor == "count":
            v = r["n"] * 3600.0 / w
        else:
            v = context.natural_value(r["vmean"], disp)
        if v is not None:
            pts.append((int(r["bin_start"]) + w / 2, float(v)))
    if len(pts) < 3:
        return ""
    W, H = 320, 56
    t0 = t1 - SPARK_SECONDS
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    span = (hi - lo) or 1.0
    xy = " ".join(f"{(t - t0) / SPARK_SECONDS * W:.1f},{H - 4 - (v - lo) / span * (H - 8):.1f}" for t, v in pts)
    mark = (int(e["bin_start"]) - t0) / SPARK_SECONDS * W
    unit = "/h" if cfg.flavor == "count" else disp.unit
    return (f'<svg class=spark viewBox="0 0 {W} {H}" role="img" aria-label="last 3 days">'
            f'<line x1="{mark:.1f}" x2="{mark:.1f}" y1="0" y2="{H}" class=mark />'
            f'<polyline points="{xy}" /></svg>'
            f'<div class="muted small">last 3 days: {context.fmt_number(lo, disp.digits)} – '
            f'{context.fmt_number(hi, disp.digits)}{_e(unit)}; the line marks this alert</div>')


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ --bg: #0b0f14; --panel: #11161d; --line: #223040; --text: #dfe6ee; --muted: #8a99a8;
          --s0: #e8a33d; --s1: #ff8a3d; --s2: #ff4d4d; --link: #7cc4ff; }}
  body {{ margin: 0 auto; max-width: 760px; padding: 16px; font: 15px/1.45 system-ui, sans-serif;
         background: var(--bg); color: var(--text); }}
  h1 {{ font-size: 18px; margin: 4px 0 12px; }} h2 {{ font-size: 13px; text-transform: uppercase;
       letter-spacing: .06em; color: var(--muted); margin: 22px 0 8px; }}
  a {{ color: var(--link); }} .muted {{ color: var(--muted); }} .small {{ font-size: 12px; }}
  table {{ border-collapse: collapse; width: 100%; }} th {{ text-align: left; vertical-align: top;
          color: var(--muted); font-weight: 500; padding: 6px 10px 6px 0; width: 6.5em; }}
  td {{ padding: 6px 0; border-top: 1px solid var(--line); }} th {{ border-top: 1px solid var(--line); }}
  .stage0 {{ color: var(--s0); }} .stage1 {{ color: var(--s1); }} .stage2 {{ color: var(--s2); }}
  .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 10px 12px;
          margin: 8px 0; }} .card .h {{ font-weight: 600; }} .card > div {{ margin: 2px 0; }}
  ul {{ padding-left: 18px; margin: 6px 0; }} li {{ margin: 3px 0; overflow-wrap: anywhere; }}
  .spark {{ width: 100%; max-width: 320px; height: 56px; display: block; margin-top: 6px; }}
  .spark polyline {{ fill: none; stroke: var(--link); stroke-width: 1.5; }}
  .spark .mark {{ stroke: var(--s2); stroke-width: 1; stroke-dasharray: 3 2; }}
  button {{ background: var(--panel); color: var(--text); border: 1px solid var(--line); border-radius: 5px;
           padding: 6px 10px; margin: 0 6px 6px 0; font: inherit; cursor: pointer; }}
</style></head><body>
<h1>{title}</h1>
<div class=muted>{links}</div>
<h2>Summary</h2><table>{summary}</table>
<h2>History</h2><ul>{history}</ul>
<h2>Signals</h2>{cards}
{news}
<h2>Was this real?</h2>
<div><button data-l="true">Real event</button><button data-l="false_positive">False alarm</button><button data-l="unclear">Unclear</button> <span id=lab class=muted></span></div>
<p class="muted small">Each signal is compared with that sensor's own normal behaviour, learned from its history
(its level, trend and daily and weekly rhythm), so "unusual" means unusual for that sensor. Alerts need
independent kinds of measurement to agree before they count as confirmed.</p>
<script>
for (const b of document.querySelectorAll("button[data-l]")) b.onclick = async () => {{
  const r = await fetch("/api/alerts/{alert_id}/label", {{method: "POST", headers: {{"Content-Type": "application/json"}},
                        body: JSON.stringify({{label: b.dataset.l}})}});
  document.getElementById("lab").textContent = r.ok ? "saved: " + b.textContent : "could not save";
}};
</script>
</body></html>
"""
