"""The nursery: a source contributes to alerts only once its PITs are calibrated.

Spec §4: a new source runs in shadow until its rolling PIT-uniformity test
passes, then it is promoted automatically; drift flags it (quarantine). The
pass criteria were left open (§15); they are set here and argued in ADR 0006.

Per stream, over the surprise rows' q_value (the randomized PIT for counts,
ADR 0003; uniform under a calibrated model) from its newest model version
only (a refitted model starts its record afresh), in a rolling window:
  n, span      enough evidence: at least MIN_N PITs spanning MIN_SPAN_DAYS
  tv           total-variation distance of the decile histogram from uniform
               (0 = flat, 0.5 = all in one decile): the shape overall
  lo, hi       how often q ≤ 1% / q ≥ 99% against the 1% expected: the tails,
               where detection lives. Only the tail(s) the stream alerts on
               ([alerts] tail) are judged.
A tail is overconfident (false alarms) when it holds more than twice its share
and the excess is significant (z > 3); underconfident (it can never fire) when
under half, significantly. Effect size AND significance: with millions of rows a
pure test rejects any model; with a few hundred, a pure ratio is noise.

  nursery     → active       over the last WINDOW_DAYS: n, span, tv ≤ TV_PASS,
                             no counted tail over- or underconfident
  active      → quarantined  over the last RECENT_DAYS: tv > TV_QUARANTINE, or a
                             counted tail more than 3× its share (significant)
  quarantined → active       the promotion test passes over the last RECENT_DAYS

Only active streams' q-values reach the alert engine (alerts.engine gates the
candidates). Every-event alerts from authoritative feeds don't use q-values and
are never gated. A stanza's status = "retired" is final; otherwise the
database's status is the source's status, written only here.
"""

from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from worldwatch.config.loader import SourceConfig
from worldwatch.instrument import record_health

WINDOW_DAYS = 14
RECENT_DAYS = 7
MIN_N = 200
MIN_SPAN_DAYS = 3.0
TV_PASS = 0.05
TV_QUARANTINE = 0.10
TAIL = 0.01
Z = 3.0
DAY = 86400

GATED = ("nursery", "quarantined")


@dataclass(frozen=True)
class Stats:
    n: int
    span_days: float
    tv: float
    lo: int  # count with q ≤ TAIL
    hi: int  # count with q ≥ 1 − TAIL

    def ratio(self, k: int) -> float:
        return k / (self.n * TAIL) if self.n else 0.0


def stats(conn: sqlite3.Connection, *since: int) -> list[dict[str, Stats]]:
    """Per-stream PIT statistics of the surprise rows after each `since` (one
    dict per window). One table scan grouped by (stream, window, decile, tail),
    summed here: a few aggregates over a few thousand groups is far cheaper in
    SQLite than many conditional sums per row."""
    bounds = sorted(set(int(s) for s in since))
    window = " + ".join(f"(bin_start >= {b})" for b in bounds)  # how many windows hold the row
    # NOT INDEXED: a table scan; walking the primary key and looking up each row
    # is several times slower (71 s against 22 s for 8.7M rows)
    sql = (f"SELECT stream_id, model_version AS v, {window} AS w, "
           f"MIN(CAST(q_value * 10 AS INTEGER), 9) AS d, "
           f"(q_value <= {TAIL}) AS lo, (q_value >= {1 - TAIL}) AS hi, COUNT(*) AS k, "
           f"MIN(bin_start) AS t0, MAX(bin_start) AS t1 FROM surprise NOT INDEXED "
           f"WHERE bin_start >= {bounds[0]} AND q_value IS NOT NULL GROUP BY 1, 2, 3, 4, 5, 6")
    rows = conn.execute(sql).fetchall()
    # a model is judged on its own PITs: a stream's newest model version only
    newest: dict[str, int] = {}
    for r in rows:
        newest[r["stream_id"]] = max(newest.get(r["stream_id"], r["v"]), r["v"])
    acc: list[dict[str, list[Any]]] = [{} for _ in bounds]  # n, t0, t1, lo, hi, deciles
    for r in rows:
        if r["v"] != newest[r["stream_id"]]:
            continue
        for i in range(int(r["w"])):  # a row in window i is in every wider one
            a = acc[i].setdefault(r["stream_id"], [0, r["t0"], r["t1"], 0, 0, [0] * 10])
            a[0] += r["k"]
            a[1], a[2] = min(a[1], r["t0"]), max(a[2], r["t1"])
            a[3] += r["k"] if r["lo"] else 0
            a[4] += r["k"] if r["hi"] else 0
            a[5][max(0, r["d"])] += r["k"]
    by_bound = {}
    for i, b in enumerate(bounds):
        by_bound[b] = {sid: Stats(n=n, span_days=(t1 - t0) / DAY,
                                  tv=0.5 * sum(abs(k / n - 0.1) for k in dec), lo=lo, hi=hi)
                       for sid, (n, t0, t1, lo, hi, dec) in acc[i].items()}
    return [by_bound[int(s)] for s in since]


def _z(k: int, s: Stats) -> float:
    e = s.n * TAIL
    return (k - e) / math.sqrt(e) if e > 0 else 0.0


def _counted(cfg: SourceConfig | None) -> tuple[str, ...]:
    tail = str(((cfg.extra.get("alerts") or {}) if cfg else {}).get("tail", "both"))
    return {"upper": ("hi",), "lower": ("lo",)}.get(tail, ("lo", "hi"))


def tail_faults(s: Stats, cfg: SourceConfig | None, over: float = 2.0,
                under: bool = True) -> list[str]:
    """The counted tails that are significantly over- (×over) or, if `under`,
    under- (×½) full."""
    faults = []
    for name in _counted(cfg):
        k = getattr(s, name)
        r, z = s.ratio(k), _z(k, s)
        if r > over and z > Z:
            faults.append(f"{name} tail {r:.1f}x (overconfident)")
        elif under and r < 0.5 and z < -Z:
            faults.append(f"{name} tail {r:.2f}x (underconfident)")
    return faults


def passes(s: Stats | None, cfg: SourceConfig | None) -> tuple[bool, str]:
    if s is None or s.n < MIN_N or s.span_days < MIN_SPAN_DAYS:
        have = "no PITs" if s is None else f"{s.n} PITs over {s.span_days:.1f} d"
        return False, f"not enough evidence yet ({have}; need {MIN_N} over {MIN_SPAN_DAYS:.0f} d)"
    faults = tail_faults(s, cfg)
    if s.tv > TV_PASS:
        faults.insert(0, f"shape off (TV {s.tv:.3f} > {TV_PASS})")
    if faults:
        return False, "; ".join(faults)
    return True, f"calibrated (n {s.n}, TV {s.tv:.3f}, tails lo {s.ratio(s.lo):.2f}x hi {s.ratio(s.hi):.2f}x)"


def drifted(s: Stats | None, cfg: SourceConfig | None) -> tuple[bool, str]:
    if s is None or s.n < MIN_N:
        return False, "too little recent evidence to judge"
    faults = tail_faults(s, cfg, over=3.0, under=False)  # a quiet tail does no harm
    if s.tv > TV_QUARANTINE:
        faults.insert(0, f"shape off (TV {s.tv:.3f} > {TV_QUARANTINE})")
    return bool(faults), "; ".join(faults) or "still calibrated"


def statuses(conn: sqlite3.Connection, sources: dict[str, SourceConfig]) -> dict[str, str]:
    """Each source's status: retired from its stanza, else the database's
    (falling back to the stanza's when the source isn't registered)."""
    db = {r["stream_id"]: r["status"] for r in conn.execute("SELECT stream_id, status FROM sources")}
    return {sid: "retired" if cfg.status == "retired" else db.get(sid, cfg.status)
            for sid, cfg in sources.items()}


def run_nursery(conn: sqlite3.Connection, sources: dict[str, SourceConfig],
                now: int | None = None) -> dict[str, int]:
    """Judge every non-retired source; record each verdict; apply the changes."""
    now = int(time.time()) if now is None else now
    window, recent = stats(conn, now - WINDOW_DAYS * DAY, now - RECENT_DAYS * DAY)
    current = statuses(conn, sources)
    changes = {"promoted": 0, "quarantined": 0, "released": 0, "judged": 0}
    for sid, cfg in sorted(sources.items()):
        status = current.get(sid, "nursery")
        if status == "retired":
            conn.execute("UPDATE sources SET status = 'retired' WHERE stream_id = ? AND status != 'retired'", (sid,))
            continue
        if status == "active":
            bad, why = drifted(recent.get(sid), cfg)
            new, event = ("quarantined", "quarantined") if bad else ("active", None)
            s = recent.get(sid)
        elif status == "quarantined":
            ok, why = passes(recent.get(sid), cfg)
            new, event = ("active", "released") if ok else ("quarantined", None)
            s = recent.get(sid)
        else:
            ok, why = passes(window.get(sid), cfg)
            new, event = ("active", "promoted") if ok else ("nursery", None)
            s = window.get(sid)
        conn.execute(
            "INSERT OR REPLACE INTO calibration (stream_id, ts, n, span_days, tv, lo_ratio, hi_ratio, "
            "status, verdict) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, now, s.n if s else 0, round(s.span_days, 2) if s else 0.0,
             round(s.tv, 4) if s else None, round(s.ratio(s.lo), 3) if s else None,
             round(s.ratio(s.hi), 3) if s else None, new, why))
        if new != status:
            conn.execute("UPDATE sources SET status = ? WHERE stream_id = ?", (new, sid))
            record_health(conn, sid, event or new, why, ts=now)
            changes[event or new] = changes.get(event or new, 0) + 1
        changes["judged"] += 1
    conn.execute("DELETE FROM calibration WHERE ts < ?", (now - 90 * DAY,))
    conn.commit()
    record_health(conn, "nursery", "ok", " ".join(f"{k}={v}" for k, v in changes.items()), ts=now)
    return changes


def latest(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The last verdict per stream, for the dashboard."""
    return [dict(r) for r in conn.execute(
        "SELECT c.* FROM calibration c JOIN (SELECT stream_id, MAX(ts) AS ts FROM calibration "
        "GROUP BY stream_id) m USING (stream_id, ts) ORDER BY c.status, c.stream_id")]
