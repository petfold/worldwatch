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
     Nursery streams are capped below waking severity.
   - every_event = true: authoritative feeds alert on each newly ingested
     item (read from the `seen` keys), one alert per region per observation.

Check-then-insert runs under BEGIN IMMEDIATE, so the live path and the sweep
can both run without opening the same alert twice (guardrail 7).
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

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


def surprisal(q: float) -> float:
    """−ln of the two-sided tail p-value of a PIT q_value (Exp(1) under H0)."""
    p = 2.0 * min(q, 1.0 - q)
    return -math.log(min(1.0, max(p, 1e-300)))


def cusum_step(s: float, q: float, k: float = DEFAULT_CUSUM_DRIFT) -> float:
    return max(0.0, s + surprisal(q) - k)


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
            for r in data:
                s = cusum_step(s, float(r["q_value"]), k)
            latest = data[-1]
            if s >= h and latest["bin_start"] >= now - recent_seconds:
                q = float(latest["q_value"])
                out.append(_anomaly(cfg, cell, scale, latest, max(q, 1 - q), s))
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
                             region, members, severity, now)
        if aid is not None:
            created.append(aid)

    created += _single_source_alerts(conn, sources, candidates, k, corr_resolution, now)
    created += _every_event_alerts(conn, sources, corr_resolution, now)
    return created


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
        for region, members in sorted(by_region.items()):
            if len({m.cell for m in members}) < int(pol.get("min_sensors", 2)):
                continue
            since = min(m.bin_start for m in members)
            severity = sum(m.extremity for m in members) / len(members)
            if cfg.status == "nursery":
                # not yet proven calibrated: visible, but never priority 5
                severity = min(severity, float(pol.get("nursery_severity_cap", 0.85)))
            aid = _insert_unless(conn, lambda r=region, s=since: _alert_since_exists(conn, r, s),
                                 region, members, severity, now)
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
            aid = _insert_unless(
                conn,
                lambda rg=region, fs=r["first_seen"]: _alert_since_exists(conn, rg, fs, sid),
                region, [member], float(pol.get("severity", 0.9)), now, kind="source_alert",
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
) -> int | None:
    """Check-then-insert atomically across processes (BEGIN IMMEDIATE)."""
    conn.commit()  # close any implicit transaction before taking the write lock
    conn.execute("BEGIN IMMEDIATE")
    try:
        if exists():
            conn.rollback()
            return None
        aid = _insert_alert(conn, region, members, severity, now, kind)
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
) -> int:
    scale = min(m.scale for m in members)
    evidence = json.dumps(
        [
            {
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
            for m in sorted(members, key=lambda m: m.extremity, reverse=True)
        ],
        separators=(",", ":"),
    )
    cur = conn.execute(
        "INSERT INTO alerts (opened_at, status, severity, cell, scale, evidence) "
        "VALUES (?, 'open', ?, ?, ?, ?)",
        (now, min(1.0, severity), region, scale, evidence),
    )
    return int(cur.lastrowid)  # type: ignore[arg-type]
