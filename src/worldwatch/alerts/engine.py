"""Alert engine (P0, with ADR 0001 policies and ADR 0002 sequential evidence).

Two layers:

1. **Candidates** — series that are anomalous *now*:
   - data series: a CUSUM on each (stream, cell, scale) series' surprisal
       e_t = −ln p_t,  p_t = 2·min(q_t, 1 − q_t)   (two-sided tail p-value)
       S_t = max(0, S_{t−1} + e_t − k)
     is at least h, and the latest row is recent. Under "nothing happening"
     e_t ~ Exp(1), so with k = 2 the statistic drifts to zero; one reading at
     p ≤ e^−(h+k) crosses at once (h = 4: p ≤ 0.0025), two consecutive at
     p ≤ e^−(h/2+k) (≈ 0.018), and so on. A dramatic observation never waits;
     a subtle one accumulates (ADR 0002 §C, replacing "≥ 2 anomalous bins").
   - silence series (presence rows, q NULL): presence_q ≥ presence_tail in
     ≥ persist_n bins including the latest (unchanged).
   Candidates come either from the surprise archive (the sweep, `run_alerts`)
   or straight from the live scorer's in-memory state (`open_alerts`).

2. **Policy** (architecture §8 + ADR 0001), applied to candidates:
   - corroboration: a coarse region's candidates span ≥ min_modalities
     modality classes. A single-stream spike never escalates.
   - role = "context": never counts toward corroboration (news).
   - single_source = true: may alert alone when ≥ min_sensors distinct cells
     of the stream in one region each carry evidence of a single reading at
     p ≤ q_tail (h = −ln q_tail − k): "confirm in space before time".
     Nursery streams are capped below waking severity. One alert per region
     and stream per episode: none again within cooldown_seconds (6 h), however
     the evidence's window moves on. max_regions: when more regions than that
     alert at once, the fault is more likely at our end (a prober losing its
     network fails everywhere at once) than in all of them: a health record
     (vantage_suspect), no alerts.
   - every_event = true: authoritative feeds alert on each newly ingested
     item (read from the `seen` keys), one alert per region per observation;
     with cooldown_seconds, none again for the region within it (a feed that
     re-reports one ongoing outage every poll).

   - provisional: a stream that needs corroboration, with one candidate strong
     enough on its own (alert_score >= PROVISIONAL_SCORE, p ~ 1e-6), opens an
     unconfirmed alert at once instead of waiting (vantage guard as above).
3. **Escalation.** An alert has a stage: 0 unconfirmed (one modality), 1
   confirmed (two or more, or a stanza marked extreme), 2 extreme (confirmed,
   and alert_score >= extreme_score()). Every run, the open alerts of the last
   ESCALATE_SECONDS gather the region's current candidates into their
   evidence; when the stage rises, the alert is updated and returned again, so
   the notifier re-pushes it at the higher priority.
Check-then-insert runs under BEGIN IMMEDIATE, so the live path and the sweep
can both run without opening the same alert twice (guardrail 7).
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import h3

from worldwatch.config.loader import SourceConfig
from worldwatch.ingest.geocode import coarsen
from worldwatch.instrument import record_health
from worldwatch.layer0.native import row_seconds

DEFAULT_CUSUM_DRIFT = 2.0  # k: a reading adds evidence only if p < e^-2 ≈ 0.14
DEFAULT_CUSUM_H = 4.0  # h: one reading at p <= 0.0025, or two at p <= ~0.018, …
DEFAULT_RECENT_SECONDS = 3 * 3600  # a candidate's latest row must be this fresh
DEFAULT_PRESENCE_TAIL = 0.9
DEFAULT_PERSIST_N = 2  # silence rows only
DEFAULT_MIN_MODALITIES = 2
DEFAULT_CORR_RESOLUTION = 2
DEFAULT_LOOKBACK_SECONDS = 24 * 3600
DEFAULT_SINGLE_SOURCE_Q_TAIL = 1e-4
DEFAULT_PER_STREAM_QUOTA = 3  # cells a stream may contribute to one region's evidence
DEFAULT_COOLDOWN_SECONDS = 6 * 3600  # single-source: no second alert per region and stream within this
DEFAULT_MAX_REGIONS = 5  # provisional alerts: more regions of one stream at once = our vantage point
PROVISIONAL_SCORE = 6.0  # one candidate this strong (p ~ 1e-6) alerts unconfirmed, at once
ESCALATE_SECONDS = 6 * 3600  # how long an alert keeps gathering confirmation


@dataclass
class Anomaly:
    stream_id: str
    cell: str
    scale: int
    bin_start: int
    q_value: float | None
    presence_q: float
    precision: float
    modality: str
    extremity: float  # 0.5..1 tail depth of the latest reading; higher = more extreme
    evidence: float = math.inf  # CUSUM statistic S (inf: silence / source alerts)
    bin_seconds: int | None = None


def tail_of(cfg: SourceConfig | None) -> str:
    """The stanza's [alerts] tail: "upper" or "lower" when only that direction can do
    harm (a dose rate, unreachable targets; night lights, traffic), else "both"."""
    return str(policy(cfg).get("tail", "both"))


def tail_p(q: float, tail: str = "both") -> float:
    """The tail p-value of a PIT q_value in the direction that counts: one-sided for
    "upper"/"lower" (a reading in the other direction is evidence of nothing), else
    two-sided. Uniform under H0 either way, so the CUSUM stays calibrated."""
    p = 1.0 - q if tail == "upper" else q if tail == "lower" else 2.0 * min(q, 1.0 - q)
    return min(1.0, max(p, 1e-300))


def one_sided_p(q: float, tail: str = "both") -> float:
    """The p-value of the tail the reading is in (for "both", the nearer tail)."""
    return tail_p(q, tail) if tail != "both" else min(q, 1.0 - q)


def tail_extremity(q: float, tail: str = "both") -> float:
    """Tail depth 0.5..1 in the direction that counts."""
    return max(0.5, q) if tail == "upper" else max(0.5, 1.0 - q) if tail == "lower" else max(q, 1.0 - q)


def surprisal(q: float, tail: str = "both") -> float:
    """−ln of the tail p-value of a PIT q_value (Exp(1) under H0)."""
    return -math.log(tail_p(q, tail))


def cusum_step(s: float, q: float, k: float = DEFAULT_CUSUM_DRIFT, tail: str = "both") -> float:
    return max(0.0, s + surprisal(q, tail) - k)


def single_source_threshold(pol: dict, k: float = DEFAULT_CUSUM_DRIFT) -> float:
    """Evidence of one reading at p <= q_tail."""
    return -math.log(float(pol.get("q_tail", DEFAULT_SINGLE_SOURCE_Q_TAIL))) - k


def policy(cfg: SourceConfig | None) -> dict:
    """The stanza's [alerts] table (empty = corroborate like everything else)."""
    return dict(cfg.extra.get("alerts", {})) if cfg is not None else {}


# --- the sweep: candidates from the surprise archive -------------------------------


def run_alerts(
    conn: sqlite3.Connection,
    sources: dict[str, SourceConfig],
    now: int | None = None,
    *,
    h: float = DEFAULT_CUSUM_H,
    k: float = DEFAULT_CUSUM_DRIFT,
    presence_tail: float = DEFAULT_PRESENCE_TAIL,
    persist_n: int = DEFAULT_PERSIST_N,
    min_modalities: int = DEFAULT_MIN_MODALITIES,
    corr_resolution: int = DEFAULT_CORR_RESOLUTION,
    lookback_seconds: int = DEFAULT_LOOKBACK_SECONDS,
    recent_seconds: int = DEFAULT_RECENT_SECONDS,
) -> list[int]:
    """Scan recent surprise rows and open alerts. Returns the ids opened this
    run. Idempotent: re-running over the same window opens nothing new."""
    run_now = now if now is not None else int(time.time())
    rows = conn.execute(
        "SELECT stream_id, cell, scale, bin_start, COALESCE(q_detect, q_value) AS q_value, "
        "presence_q, precision "
        "FROM surprise WHERE bin_start >= ? ORDER BY stream_id, cell, scale, bin_start",
        (run_now - lookback_seconds,),
    ).fetchall()
    candidates = candidates_from_rows(
        rows, sources, run_now, h=h, k=k, presence_tail=presence_tail,
        persist_n=persist_n, recent_seconds=recent_seconds,
    )
    created = open_alerts(
        conn, sources, candidates, run_now, h=h, k=k,
        min_modalities=min_modalities, corr_resolution=corr_resolution,
    )
    record_health(conn, "alerts", "ok", f"opened={len(created)}", ts=run_now)
    return created


def candidates_from_rows(
    rows: Iterable[sqlite3.Row],
    sources: dict[str, SourceConfig],
    now: int,
    *,
    h: float = DEFAULT_CUSUM_H,
    k: float = DEFAULT_CUSUM_DRIFT,
    presence_tail: float = DEFAULT_PRESENCE_TAIL,
    persist_n: int = DEFAULT_PERSIST_N,
    recent_seconds: int = DEFAULT_RECENT_SECONDS,
) -> list[Anomaly]:
    series: dict[tuple[str, str, int], list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        series[(r["stream_id"], r["cell"], r["scale"])].append(r)

    out: list[Anomaly] = []
    for (stream_id, cell, scale), rs in series.items():
        cfg = sources.get(stream_id)
        if cfg is None or cfg.status == "retired":
            continue
        data = [r for r in rs if r["q_value"] is not None]
        silence = [r for r in rs if r["q_value"] is None]
        if data:
            s = 0.0
            tail = tail_of(cfg)
            for r in data:
                s = cusum_step(s, float(r["q_value"]), k, tail)
            latest = data[-1]
            if s >= h and latest["bin_start"] >= now - recent_seconds:
                q = float(latest["q_value"])
                out.append(_anomaly(cfg, cell, scale, latest, tail_extremity(q, tail), s))
        if silence and silence[-1]["presence_q"] >= presence_tail:
            if sum(r["presence_q"] >= presence_tail for r in silence) >= persist_n:
                out.append(_anomaly(cfg, cell, scale, silence[-1], float(silence[-1]["presence_q"])))
    return out


def _anomaly(
    cfg: SourceConfig, cell: str, scale: int, row: sqlite3.Row, extremity: float,
    evidence: float = math.inf,
) -> Anomaly:
    return Anomaly(
        stream_id=cfg.stream_id, cell=cell, scale=scale, bin_start=int(row["bin_start"]),
        q_value=row["q_value"], presence_q=float(row["presence_q"]),
        precision=float(row["precision"]), modality=cfg.modality, extremity=extremity,
        evidence=evidence, bin_seconds=row_seconds(cfg, scale),
    )


# --- policy: candidates → alerts ---------------------------------------------------


def open_alerts(
    conn: sqlite3.Connection,
    sources: dict[str, SourceConfig],
    candidates: list[Anomaly],
    now: int,
    *,
    h: float = DEFAULT_CUSUM_H,
    k: float = DEFAULT_CUSUM_DRIFT,
    min_modalities: int = DEFAULT_MIN_MODALITIES,
    corr_resolution: int = DEFAULT_CORR_RESOLUTION,
    per_stream: int = DEFAULT_PER_STREAM_QUOTA,
) -> list[int]:
    """Apply the corroboration and per-source policies. Shared by the sweep
    and the live path; idempotent."""
    created: list[int] = []

    corroborating = [
        c for c in candidates
        if c.evidence >= h and policy(sources.get(c.stream_id)).get("role") != "context"
    ]
    regions: dict[str, list[Anomaly]] = defaultdict(list)
    for c in corroborating:
        regions[coarsen(c.cell, corr_resolution)].append(c)
    for region, members in sorted(regions.items()):
        members = _quota(members, per_stream)
        modalities = {m.modality for m in members}
        if len(modalities) < min_modalities:
            continue
        mean_ext = sum(m.extremity for m in members) / len(members)
        severity = min(1.0, mean_ext * len(modalities) / min_modalities)
        aid = _insert_unless(conn, lambda r=region: _open_alert_exists(conn, r),
                             region, members, severity, now, sources=sources)
        if aid is not None:
            created.append(aid)

    created += _single_source_alerts(conn, sources, candidates, k, corr_resolution, now)
    created += _every_event_alerts(conn, sources, corr_resolution, now)
    created += _provisional_alerts(conn, sources, candidates, corr_resolution, now)
    escalated = _escalations(conn, sources, candidates, now)
    return created + [a for a in escalated if a not in created]


def _quota(members: list[Anomaly], per_stream: int) -> list[Anomaly]:
    """Coverage balance (ADR 0002 §G): a stream contributes at most `per_stream`
    cells to one region's evidence (the strongest), so a densely sampled
    network can't dominate an alert by volume."""
    by_stream: dict[str, list[Anomaly]] = defaultdict(list)
    for m in members:
        by_stream[m.stream_id].append(m)
    out: list[Anomaly] = []
    for ms in by_stream.values():
        out += sorted(ms, key=lambda m: m.evidence, reverse=True)[:per_stream]
    return out


def _single_source_alerts(
    conn: sqlite3.Connection,
    sources: dict[str, SourceConfig],
    candidates: list[Anomaly],
    k: float,
    corr_resolution: int,
    now: int,
) -> list[int]:
    """Streams allowed to alert alone, confirmed by independent sensors of
    their own network at the same time: a lone faulty detector never passes
    min_sensors, and agreement in space replaces waiting in time."""
    created: list[int] = []
    for sid, cfg in sorted(sources.items()):
        pol = policy(cfg)
        if not pol.get("single_source") or cfg.status == "retired":
            continue
        need = single_source_threshold(pol, k)
        own = [c for c in candidates if c.stream_id == sid and c.q_value is not None
               and c.evidence >= need]
        by_region: dict[str, list[Anomaly]] = defaultdict(list)
        for c in own:
            by_region[coarsen(c.cell, int(pol.get("region_resolution", corr_resolution)))].append(c)
        alerting = [(region, members) for region, members in sorted(by_region.items())
                    if len({m.cell for m in members}) >= int(pol.get("min_sensors", 2))]
        max_regions = pol.get("max_regions")
        if max_regions is not None and len(alerting) > int(max_regions):
            # everywhere at once: more likely our vantage point than every region
            record_health(conn, sid, "vantage_suspect", f"regions={len(alerting)}", ts=now)
            continue
        cooldown = int(pol.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS))
        for region, members in alerting:
            # the episode, not the latest window: a persisting anomaly re-scored every
            # round must not open a new alert each round
            since = min(min(m.bin_start for m in members), now - cooldown)
            severity = sum(m.extremity for m in members) / len(members)
            if cfg.status == "nursery":
                # not yet proven calibrated: visible, but never priority 5
                severity = min(severity, float(pol.get("nursery_severity_cap", 0.85)))
            aid = _insert_unless(conn, lambda r=region, s=since: _alert_since_exists(conn, r, s, sid),
                                 region, members, severity, now, sources=sources)
            if aid is not None:
                created.append(aid)
    return created


def _every_event_alerts(
    conn: sqlite3.Connection,
    sources: dict[str, SourceConfig],
    corr_resolution: int,
    now: int,
) -> list[int]:
    """Authoritative feeds: each newly ingested item is an alert. Reads the
    ingestion keys (`seen`), not values."""
    created: list[int] = []
    for sid, cfg in sorted(sources.items()):
        pol = policy(cfg)
        if not pol.get("every_event") or cfg.status == "retired":
            continue
        fresh_window = int(pol.get("fresh_seconds", 6 * 3600))
        cooldown = pol.get("cooldown_seconds")  # opt-in: distinct events may share a region
        for r in conn.execute(
            "SELECT cell, ts, first_seen FROM seen WHERE stream_id = ? AND first_seen >= ? "
            "AND ts >= ? ORDER BY ts",
            (sid, now - fresh_window, now - fresh_window),
        ).fetchall():
            region = coarsen(r["cell"], corr_resolution)
            member = Anomaly(
                stream_id=sid, cell=r["cell"], scale=0, bin_start=r["ts"], q_value=None,
                presence_q=1.0, precision=1.0, modality=cfg.modality, extremity=1.0,
                bin_seconds=0,
            )
            since = r["first_seen"] if cooldown is None else min(r["first_seen"], now - int(cooldown))
            aid = _insert_unless(
                conn,
                lambda rg=region, fs=since: _alert_since_exists(conn, rg, fs, sid),
                region, [member], float(pol.get("severity", 0.9)), now, kind="source_alert",
                sources=sources,
            )
            if aid is not None:
                created.append(aid)
    return created


def _alert_since_exists(
    conn: sqlite3.Connection, region: str, since: int, stream_id: str | None = None
) -> bool:
    rows = conn.execute(
        "SELECT evidence FROM alerts WHERE cell = ? AND opened_at >= ?", (region, since)
    ).fetchall()
    if stream_id is None:
        return bool(rows)
    return any(e.get("stream_id") == stream_id for r in rows for e in json.loads(r["evidence"]))


def _open_alert_exists(conn: sqlite3.Connection, region: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM alerts WHERE cell = ? AND status = 'open' LIMIT 1", (region,)
        ).fetchone()
        is not None
    )


def _insert_unless(
    conn: sqlite3.Connection,
    exists: Callable[[], bool],
    region: str,
    members: list[Anomaly],
    severity: float,
    now: int,
    kind: str | None = None,
    sources: dict[str, SourceConfig] | None = None,
) -> int | None:
    """Check-then-insert atomically across processes (BEGIN IMMEDIATE)."""
    conn.commit()  # close any implicit transaction before taking the write lock
    conn.execute("BEGIN IMMEDIATE")
    try:
        if exists():
            conn.rollback()
            return None
        aid = _insert_alert(conn, region, members, severity, now, kind, sources)
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return aid


def _insert_alert(
    conn: sqlite3.Connection,
    region: str,
    members: list[Anomaly],
    severity: float,
    now: int,
    kind: str | None = None,
    sources: dict[str, SourceConfig] | None = None,
) -> int:
    scale = min(m.scale for m in members)
    ev = [_member(m, kind) for m in sorted(members, key=lambda m: m.extremity, reverse=True)]
    cur = conn.execute(
        "INSERT INTO alerts (opened_at, status, severity, cell, scale, evidence, stage) "
        "VALUES (?, 'open', ?, ?, ?, ?, ?)",
        (now, min(1.0, severity), region, scale, json.dumps(ev, separators=(",", ":")),
         stage_of(ev, sources)),
    )
    return int(cur.lastrowid)  # type: ignore[arg-type]


def _member(m: Anomaly, kind: str | None = None) -> dict:
    """An alert's evidence entry for one anomaly."""
    return {
        "stream_id": m.stream_id,
        "cell": m.cell,
        "scale": m.scale,
        "bin_start": m.bin_start,
        "bin_seconds": m.bin_seconds,
        "q_value": m.q_value,
        "presence_q": m.presence_q,
        "precision": m.precision,
        "modality": m.modality,
        **({"evidence": round(m.evidence, 2)} if math.isfinite(m.evidence) else {}),
        **({"kind": kind} if kind else {}),
    }


# --- provisional alerts and escalation (alert early, raise the priority as confirmation comes)


def _provisional_alerts(
    conn: sqlite3.Connection,
    sources: dict[str, SourceConfig],
    candidates: list[Anomaly],
    corr_resolution: int,
    now: int,
) -> list[int]:
    """Streams that need corroboration, with one candidate strong enough on its own:
    an unconfirmed alert now, not silence until a second modality agrees. Escalation
    raises it when one does."""
    created: list[int] = []
    strong: dict[str, dict[str, list[Anomaly]]] = defaultdict(lambda: defaultdict(list))
    for c in candidates:
        pol = policy(sources.get(c.stream_id))
        if (c.q_value is None or pol.get("role") == "context" or pol.get("single_source")
                or pol.get("every_event")):
            continue
        if alert_score([_member(c)], sources)[0] >= PROVISIONAL_SCORE:
            strong[c.stream_id][coarsen(c.cell, corr_resolution)].append(c)
    for sid, by_region in sorted(strong.items()):
        pol = policy(sources.get(sid))
        if len(by_region) > int(pol.get("max_regions", DEFAULT_MAX_REGIONS)):
            record_health(conn, sid, "vantage_suspect", f"regions={len(by_region)}", ts=now)
            continue
        cooldown = int(pol.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS))
        for region, members in sorted(by_region.items()):
            aid = _insert_unless(
                conn,
                lambda r=region: _open_alert_exists(conn, r) or _alert_since_exists(conn, r, now - cooldown, sid),
                region, members, 0.6, now, kind="provisional", sources=sources,
            )
            if aid is not None:
                created.append(aid)
    return created


def _escalations(
    conn: sqlite3.Connection,
    sources: dict[str, SourceConfig],
    candidates: list[Anomaly],
    now: int,
) -> list[int]:
    """Open alerts of the last ESCALATE_SECONDS gather the current candidates of their
    region (data rows only: a silent feed confirms nothing); where the stage rises, the
    alert is updated (a conditional UPDATE: one process escalates it once)."""
    rows = conn.execute(
        "SELECT alert_id, cell, severity, evidence, stage FROM alerts "
        "WHERE status = 'open' AND opened_at >= ? AND stage < 2", (now - ESCALATE_SECONDS,),
    ).fetchall()
    escalated: list[int] = []
    for r in rows:
        ev = json.loads(r["evidence"])
        have = {(e.get("stream_id"), e.get("cell")) for e in ev}
        extra = [_member(c) for c in candidates if c.q_value is not None
                 and policy(sources.get(c.stream_id)).get("role") != "context"  # news: never evidence
                 and (c.stream_id, c.cell) not in have and _within(c.cell, r["cell"])]
        if not extra:
            continue
        merged = ev + extra
        stage = stage_of(merged, sources)
        if stage <= r["stage"]:
            continue
        for e in extra:  # the alert's history, for its report
            e.update(added_at=now, stage=stage)
        severity = max(float(r["severity"]), 0.95 if stage == 2 else 0.8)
        cur = conn.execute(
            "UPDATE alerts SET evidence = ?, stage = ?, severity = ?, escalated_at = ? "
            "WHERE alert_id = ? AND stage < ?",
            (json.dumps(merged, separators=(",", ":")), stage, severity, now, r["alert_id"], stage),
        )
        conn.commit()
        if cur.rowcount:
            escalated.append(int(r["alert_id"]))
    return escalated


def _within(cell: str, region: str) -> bool:
    """Whether a cell lies in an alert's region (non-H3 cells: only itself)."""
    if not (h3.is_valid_cell(region) and h3.is_valid_cell(cell)):
        return cell == region
    res = h3.get_resolution(region)
    return h3.get_resolution(cell) >= res and coarsen(cell, res) == region


# --- how serious an alert is: what the push budget ranks by ---------------------------------

SCORE_P_FLOOR = 1e-15  # q_values this extreme are indistinguishable anyway


def alert_score(evidence: list[dict], sources: dict[str, SourceConfig] | None) -> tuple[float, int, bool]:
    """(score, modalities, extreme-eligible) of an alert's evidence.

    score: how improbable the evidence is under normal conditions, −log10 of each
    independent cell's two-sided tail p (the strongest per stream and cell), summed;
    an every-item feed's member counts its stanza's push_score instead (default 5).
    Confirmation by independent kinds of measurement multiplies it: × the number of
    modalities, when two or more. extreme-eligible (confirmed): two modalities or more,
    an item an authoritative feed issued (every_event), or a stanza that says extreme =
    true confirming it on its own terms: at least its min_sensors cells (default 1)
    beyond its q_tail (default 1e-4) — weak readings of a radiation network merged
    into an alert confirm nothing (score thresholds are the notifier's).
    """
    per_cell: dict[tuple[str, str], float] = {}
    feed = 0.0
    strong: dict[str, set[str]] = defaultdict(set)  # extreme stanzas: their confirming cells
    issued = False
    for e in evidence:
        pol = policy((sources or {}).get(e.get("stream_id", "")))
        q = e.get("q_value")
        if e.get("kind") == "source_alert":
            issued = True
        elif pol.get("extreme") and q is not None and one_sided_p(float(q), str(pol.get("tail", "both"))) \
                <= float(pol.get("q_tail", 1e-4)):
            strong[e.get("stream_id", "")].add(e.get("cell") or "")
        if q is None:
            if e.get("kind") == "source_alert":
                feed += float(pol.get("push_score", 5.0))
            continue
        p = max(tail_p(float(q), str(pol.get("tail", "both"))), SCORE_P_FLOOR)
        key = (e.get("stream_id", ""), e.get("cell", ""))
        per_cell[key] = max(per_cell.get(key, 0.0), -math.log10(p))
    modalities = len({e.get("modality") for e in evidence})
    score = (sum(per_cell.values()) + feed) * (modalities if modalities >= 2 else 1)
    extreme = any(len(cells) >= int(policy((sources or {}).get(sid)).get("min_sensors", 1))
                  for sid, cells in strong.items())
    return score, modalities, issued or extreme or modalities >= 2


def extreme_score() -> float:
    """The score an alert must reach, confirmed, to be extreme (stage 2): may wake."""
    return float(os.environ.get("WW_PUSH_EXTREME_SCORE", "15"))


def stage_of(evidence: list[dict], sources: dict[str, SourceConfig] | None) -> int:
    """0 unconfirmed (one modality), 1 confirmed (two or more, or a stanza marked
    extreme), 2 extreme (confirmed, and alert_score >= extreme_score())."""
    score, _, confirmed = alert_score(evidence, sources)
    if not confirmed:
        return 0
    return 2 if score >= extreme_score() else 1
