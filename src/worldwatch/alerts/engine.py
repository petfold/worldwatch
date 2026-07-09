"""Naive corroborated alert engine (P0).

Reads the surprise archive and opens an alert only when all three hold
(architecture §8), which is what makes q_values worth combining (P6):

  - persistence:   a (stream, cell, scale) is anomalous in >= persist_n bins
                   within the look-back, including its most recent bin
  - geographic coherence: anomalies are grouped by a common coarse H3 region
                   (nearby fine cells from different feeds collapse together);
                   non-spatial feeds group under their literal cell (GLOBAL,
                   entity, or the _PRESENCE_ silence sentinel)
  - corroboration: the region's persistent anomalies span >= min_modalities
                   distinct modality classes (physical/economic/…)

A single-stream spike therefore never escalates (one modality). Presence
silence rows participate identically: multi-source silence is a loud alarm.

Naive vs P1: persistence is a simple count (not a run test), corroboration is a
modality count (not precision-weighted evidence combination), and there is no
explained-away discount yet. All deferred to Layer 1 / P1.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass

from worldwatch.config.loader import SourceConfig
from worldwatch.ingest.geocode import coarsen
from worldwatch.instrument import record_health

DEFAULT_Q_TAIL = 0.99  # two-sided: anomalous if q >= this or q <= 1 - this
DEFAULT_PRESENCE_TAIL = 0.9
DEFAULT_PERSIST_N = 2
DEFAULT_MIN_MODALITIES = 2
DEFAULT_CORR_RESOLUTION = 2
DEFAULT_LOOKBACK_SECONDS = 24 * 3600


@dataclass
class _Anomaly:
    stream_id: str
    cell: str
    scale: int
    bin_start: int
    q_value: float | None
    presence_q: float
    precision: float
    modality: str
    extremity: float  # 0.5..1 tail depth; higher = more extreme


def run_alerts(
    conn: sqlite3.Connection,
    sources: dict[str, SourceConfig],
    now: int | None = None,
    *,
    q_tail: float = DEFAULT_Q_TAIL,
    presence_tail: float = DEFAULT_PRESENCE_TAIL,
    persist_n: int = DEFAULT_PERSIST_N,
    min_modalities: int = DEFAULT_MIN_MODALITIES,
    corr_resolution: int = DEFAULT_CORR_RESOLUTION,
    lookback_seconds: int = DEFAULT_LOOKBACK_SECONDS,
) -> list[int]:
    """Scan recent surprise rows and open corroborated alerts. Returns the ids
    of alerts opened this run. Idempotent: a region with an already-open alert
    is not re-opened."""
    run_now = now if now is not None else int(time.time())
    cutoff = run_now - lookback_seconds

    rows = conn.execute(
        "SELECT stream_id, cell, scale, bin_start, q_value, presence_q, precision "
        "FROM surprise WHERE bin_start >= ? ORDER BY stream_id, cell, scale, bin_start",
        (cutoff,),
    ).fetchall()

    persistent = _persistent_anomalies(rows, sources, q_tail, presence_tail, persist_n)

    # Group by coarse region for corroboration.
    regions: dict[str, list[_Anomaly]] = defaultdict(list)
    for a in persistent:
        regions[coarsen(a.cell, corr_resolution)].append(a)

    created: list[int] = []
    for region, members in sorted(regions.items()):
        modalities = {m.modality for m in members}
        if len(modalities) < min_modalities:
            continue
        if _open_alert_exists(conn, region):
            continue
        created.append(_open_alert(conn, region, members, modalities, min_modalities, run_now))

    record_health(conn, "alerts", "ok", f"opened={len(created)}", ts=run_now)
    return created


def _persistent_anomalies(
    rows: list[sqlite3.Row],
    sources: dict[str, SourceConfig],
    q_tail: float,
    presence_tail: float,
    persist_n: int,
) -> list[_Anomaly]:
    series: dict[tuple[str, str, int], list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        series[(r["stream_id"], r["cell"], r["scale"])].append(r)

    out: list[_Anomaly] = []
    for (stream_id, cell, scale), rs in series.items():
        cfg = sources.get(stream_id)
        if cfg is None or cfg.status == "retired":
            continue
        extremities = [_extremity(r, q_tail, presence_tail) for r in rs]
        if extremities[-1] is None:  # latest bin must be anomalous
            continue
        if sum(e is not None for e in extremities) < persist_n:
            continue
        latest = rs[-1]
        out.append(
            _Anomaly(
                stream_id=stream_id,
                cell=cell,
                scale=scale,
                bin_start=latest["bin_start"],
                q_value=latest["q_value"],
                presence_q=latest["presence_q"],
                precision=latest["precision"],
                modality=cfg.modality,
                extremity=extremities[-1],
            )
        )
    return out


def _extremity(row: sqlite3.Row, q_tail: float, presence_tail: float) -> float | None:
    """Tail depth (0.5..1) if the row is anomalous, else None.

    A data row (q_value present) is judged on its q_value tail; the runner's
    placeholder presence_q=1.0 is ignored. A silence row (q_value NULL) is
    judged on presence_q.
    """
    q = row["q_value"]
    if q is not None:
        if q >= q_tail:
            return float(q)
        if q <= 1.0 - q_tail:
            return float(1.0 - q)
        return None
    pq = row["presence_q"]
    if pq is not None and pq >= presence_tail:
        return float(pq)
    return None


def _open_alert_exists(conn: sqlite3.Connection, region: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM alerts WHERE cell = ? AND status = 'open' LIMIT 1", (region,)
        ).fetchone()
        is not None
    )


def _open_alert(
    conn: sqlite3.Connection,
    region: str,
    members: list[_Anomaly],
    modalities: set[str],
    min_modalities: int,
    now: int,
) -> int:
    mean_ext = sum(m.extremity for m in members) / len(members)
    severity = min(1.0, mean_ext * len(modalities) / min_modalities)
    scale = min(m.scale for m in members)
    evidence = json.dumps(
        [
            {
                "stream_id": m.stream_id,
                "cell": m.cell,
                "scale": m.scale,
                "bin_start": m.bin_start,
                "q_value": m.q_value,
                "presence_q": m.presence_q,
                "precision": m.precision,
                "modality": m.modality,
            }
            for m in sorted(members, key=lambda m: m.extremity, reverse=True)
        ],
        separators=(",", ":"),
    )
    cur = conn.execute(
        "INSERT INTO alerts (opened_at, status, severity, cell, scale, evidence) "
        "VALUES (?, 'open', ?, ?, ?, ?)",
        (now, severity, region, scale, evidence),
    )
    conn.commit()
    return int(cur.lastrowid)  # type: ignore[arg-type]
