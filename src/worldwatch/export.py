"""Daily Parquet export of the permanent record, for the local machine (spec §6).

What lasts is exported; what rolls over is not (raw_ring: the fine window;
seen: 8 days; context: the evidence store's fixed budget). Two kinds of file:

  <dir>/<table>/batch-NNNNNN.parquet   surprise and bins: the rows written since
                                        the previous export, found by rowid. A
                                        row rewritten later (INSERT OR REPLACE
                                        gives it a new rowid) comes again in a
                                        later batch, so a reader keeps, per
                                        primary key, the row with the highest
                                        (_rowid, _batch).
  <dir>/snapshots/<table>.parquet       the small permanent tables, whole, every run.

Each batch starts at the previous run's highest rowid, inclusive: SQLite can
hand a replaced row its old rowid back when that row was the newest, and the
one repeated row costs nothing. `_state.json` (batch number and rowid marks)
is written last, so a run that dies re-exports the same batch under the same
file names (guardrail 7). Every table is read in one transaction: a run is
one consistent moment of the database. Nothing here deletes an export file.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

INCREMENTAL = ("surprise", "bins")
SNAPSHOTS = ("sources", "model_state", "presence_state", "alerts", "push_log", "digests", "health")
_ROWS_PER_GROUP = 200_000
_STATE = "_state.json"


def export(db: Path, out: Path, now: int | None = None) -> dict[str, int]:
    """Export to `out`; returns rows written per table."""
    import pyarrow as pa  # the `export` extra; only this job needs it
    import pyarrow.parquet as pq

    state = _read_state(out)
    batch = int(state["batch"]) + 1
    marks = dict(state["marks"])
    counts: dict[str, int] = {}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        conn.execute("BEGIN")  # one read snapshot for every table
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in INCREMENTAL:
            if table not in have:
                continue
            hi = conn.execute(f"SELECT MAX(rowid) FROM {table}").fetchone()[0]
            if hi is None:
                continue
            lo = int(marks.get(table, 0))
            counts[table] = _write(
                pa, pq, conn, table,
                f"SELECT rowid AS _rowid, {batch} AS _batch, * FROM {table} "
                "WHERE rowid >= ? AND rowid <= ? ORDER BY rowid",
                (lo, hi), out / table / f"batch-{batch:06d}.parquet",
                extra=[("_rowid", pa.int64()), ("_batch", pa.int64())],
            )
            marks[table] = hi
        for table in SNAPSHOTS:
            if table in have:
                counts[table] = _write(pa, pq, conn, table, f"SELECT * FROM {table}", (),
                                       out / "snapshots" / f"{table}.parquet")
        conn.rollback()
    finally:
        conn.close()
    _write_state(out, {"batch": batch, "marks": marks,
                       "exported_at": int(time.time()) if now is None else now})
    return counts


def _write(pa: Any, pq: Any, conn: sqlite3.Connection, table: str, sql: str,
           params: tuple, path: Path, extra: list | None = None) -> int:
    fields = list(extra or []) + [(c[1], _arrow_type(pa, c[2])) for c in conn.execute(f"PRAGMA table_info({table})")]
    schema = pa.schema(fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    n = 0
    cur = conn.execute(sql, params)
    with pq.ParquetWriter(tmp, schema, compression="zstd") as writer:
        while rows := cur.fetchmany(_ROWS_PER_GROUP):
            cols = list(zip(*rows, strict=True))
            writer.write_table(pa.table([pa.array(col, type=f.type) for col, f in zip(cols, schema, strict=True)], schema=schema))
            n += len(rows)
        if n == 0:
            writer.write_table(schema.empty_table())
    os.replace(tmp, path)
    return n


def _arrow_type(pa: Any, declared: str) -> Any:
    """SQLite's type affinity rules, mapped to Arrow."""
    d = declared.upper()
    if "INT" in d:
        return pa.int64()
    if any(k in d for k in ("CHAR", "CLOB", "TEXT")):
        return pa.string()
    if "BLOB" in d or not d:
        return pa.binary()
    return pa.float64()


def _read_state(out: Path) -> dict[str, Any]:
    try:
        return json.loads((out / _STATE).read_text())
    except FileNotFoundError:
        return {"batch": 0, "marks": {}}


def _write_state(out: Path, state: dict[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / (_STATE + ".tmp")
    tmp.write_text(json.dumps(state, indent=1) + "\n")
    os.replace(tmp, out / _STATE)
