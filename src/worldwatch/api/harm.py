"""Harm levels: is what was observed dangerous, not only unusual? (push policy, ADR 0004)

Detection is surprise on q_values alone (the interchange contract): Chernobyl's
exclusion zone reads 5 µSv/h every hour and is rightly not surprising, and a
station 5% above its own normal is surprising but harmless. Whether a person
should be told needs the value itself, so harm is judged here, where pushes are
decided and the cascade's bins are already read for their text — never in the
alert engine or Layer 1.

A stanza's [alerts] table may give:

    harm_levels    = [[0.3, "above natural background"], [1.0, "..."], ...]
    harm_below     = "within natural background"   # words for level 0
    harm_value     = "max"      # the bin statistic, in the source's units: max | mean
    harm_floor     = true       # level 0 is not worth a push (the signal is dropped)
    harm_confirmed = 2          # this level, confirmed: at least Confirmed, pushed past the budget
    harm_extreme   = 3          # this level, confirmed: Extreme

Level n = the number of thresholds the value reaches. A level is confirmed when
min_sensors independent cells of the stream reach it (one detector can fail), or
when two kinds of measurement already agree on the alert.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from worldwatch.api import context
from worldwatch.config.loader import SourceConfig


@dataclass(frozen=True)
class MemberHarm:
    level: int
    value: float
    label: str


@dataclass(frozen=True)
class Harm:
    kept: list[dict]  # the evidence that counts for pushing
    dropped: list[dict]  # signals below their harm floor: interesting, not for alerting
    level: int = 0  # the highest confirmed level
    label: str = ""  # its words ("" if no stream has levels)
    stage: int = -1  # the stage it implies (1 Confirmed, 2 Extreme; -1 none)
    peak: int = 0  # the highest single reading's level (may be one faulty detector)
    peak_label: str = ""
    members: dict[int, MemberHarm] = field(default_factory=dict)  # by index in the evidence


def _policy(cfg: SourceConfig | None) -> dict:
    from worldwatch.alerts.engine import policy

    return policy(cfg)


def member_harm(conn: sqlite3.Connection | None, e: dict, cfg: SourceConfig | None) -> MemberHarm | None:
    """The harm level of one signal's observed value; None if its stanza has no levels
    or the value is not known."""
    pol = _policy(cfg)
    levels = pol.get("harm_levels")
    if not levels or conn is None or not e.get("cell") or e.get("q_value") is None:
        return None
    stat = "max" if pol.get("harm_value", "max") == "max" else "mean"
    raw = None
    if e.get("bin_start") is not None:  # the reading itself, while it is still raw
        t0 = int(e["bin_start"])
        raw = conn.execute(
            f"SELECT {'MAX' if stat == 'max' else 'AVG'}(value) FROM raw_ring "
            "WHERE stream_id = ? AND cell = ? AND ts >= ? AND ts < ?",
            (e["stream_id"], e["cell"], t0, t0 + max(int(e.get("bin_seconds") or 0), 1))).fetchone()[0]
    if raw is None:
        row = context.bin_row(conn, e["stream_id"], e["cell"], e.get("scale"), e.get("bin_start"))
        if row is None:
            return None
        raw = row["vmax"] if stat == "max" else row["vmean"]
    value = context.natural_value(raw, context.display_for(e["stream_id"], cfg))
    if value is None:
        return None
    level = sum(value >= float(t) for t, _ in levels)
    label = str(levels[level - 1][1]) if level else str(pol.get("harm_below", "below harm levels"))
    return MemberHarm(level, value, label)


def assess(conn: sqlite3.Connection | None, evidence: list[dict],
           sources: dict[str, SourceConfig] | None) -> Harm:
    sources = sources or {}
    members: dict[int, MemberHarm] = {}
    kept, dropped = [], []
    for i, e in enumerate(evidence):
        cfg = sources.get(e.get("stream_id", ""))
        mh = member_harm(conn, e, cfg)
        if mh is not None:
            members[i] = mh
            if mh.level == 0 and _policy(cfg).get("harm_floor", True):
                dropped.append(e)
                continue
        kept.append(e)
    if not members:
        return Harm(kept, dropped)
    corroborated = len({e.get("modality") for e in kept}) >= 2
    top = max(members.values(), key=lambda mh: mh.level)
    best, best_label, stage = 0, "", -1
    for sid in sorted({evidence[i]["stream_id"] for i in members}):
        pol = _policy(sources.get(sid))
        mine = [(evidence[i].get("cell"), mh) for i, mh in members.items() if evidence[i]["stream_id"] == sid]
        need = 1 if corroborated else int(pol.get("min_sensors", 1))
        for lvl in sorted({mh.level for _, mh in mine}, reverse=True):
            if len({c for c, mh in mine if mh.level >= lvl}) >= need:
                if lvl > best or not best_label:
                    best = lvl
                    best_label = (next(mh.label for _, mh in mine if mh.level == lvl) if lvl
                                  else str(pol.get("harm_below", "below harm levels")))
                if lvl >= int(pol.get("harm_extreme", 10**9)):
                    stage = max(stage, 2)
                elif lvl >= int(pol.get("harm_confirmed", 10**9)):
                    stage = max(stage, 1)
                break
    return Harm(kept, dropped, best, best_label, stage, members=members, peak=top.level, peak_label=top.label)
