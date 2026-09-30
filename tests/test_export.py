"""The daily Parquet export: the permanent record, incremental and safe to re-run."""

import json

import pytest

pq = pytest.importorskip("pyarrow.parquet")

from worldwatch.cascade.consolidator import _fold_group  # noqa: E402
from worldwatch.export import export  # noqa: E402

KEY = ("stream_id", "cell", "scale", "bin_start")


def _surprise(db, n, start=0):
    db.executemany(
        "INSERT INTO surprise (stream_id, cell, scale, bin_start, q_value, presence_q, precision, n_obs, "
        "model_version) VALUES ('s', 'c', -1, ?, 0.5, 0.5, 1.0, 1, 1)",
        [(1790000000 + 60 * i,) for i in range(start, start + n)],
    )
    db.commit()


def _latest(out, table):
    """What a reader sees: per primary key, the row with the highest (_rowid, _batch)."""
    best: dict[tuple, dict] = {}
    for r in pq.read_table(out / table).to_pylist():
        k = tuple(r[c] for c in KEY)
        if k not in best or (r["_rowid"], r["_batch"]) > (best[k]["_rowid"], best[k]["_batch"]):
            best[k] = r
    return best


def test_each_run_exports_only_what_was_written_since(db, tmp_path):
    out = tmp_path / "export"
    _surprise(db, 5)
    assert export(tmp_path / "test.db", out)["surprise"] == 5
    _surprise(db, 3, start=5)
    assert export(tmp_path / "test.db", out)["surprise"] == 4  # 3 new + the previous run's last row
    assert sorted(p.name for p in (out / "surprise").iterdir()) == ["batch-000001.parquet", "batch-000002.parquet"]
    assert len(_latest(out, "surprise")) == 8


def test_a_rewritten_bin_comes_again_and_the_newest_wins(db, tmp_path):
    out = tmp_path / "export"
    _fold_group(db, "s", "c", 3, 1790000000, [1.0, 2.0])
    _fold_group(db, "s", "c", 3, 1790003600, [5.0])
    db.commit()
    export(tmp_path / "test.db", out)
    _fold_group(db, "s", "c", 3, 1790000000, [3.0])  # a late observation merged into the first bin
    db.commit()
    export(tmp_path / "test.db", out)
    latest = _latest(out, "bins")
    assert len(latest) == 2
    first = latest[("s", "c", 3, 1790000000)]
    assert first["n"] == 3 and first["vmean"] == pytest.approx(2.0)
    assert first["sketch"]  # BLOBs survive


def test_a_rewritten_newest_bin_keeps_its_rowid_and_is_still_exported(db, tmp_path):
    """SQLite gives a replaced row its old rowid back when it was the newest:
    each batch starts at the previous run's last rowid, inclusive."""
    out = tmp_path / "export"
    _fold_group(db, "s", "c", 3, 1790000000, [1.0])
    _fold_group(db, "s", "c", 3, 1790003600, [5.0])
    db.commit()
    export(tmp_path / "test.db", out)
    _fold_group(db, "s", "c", 3, 1790003600, [7.0])
    db.commit()
    export(tmp_path / "test.db", out)
    assert _latest(out, "bins")[("s", "c", 3, 1790003600)]["n"] == 2


def test_a_run_that_dies_before_its_state_repeats_the_same_batch(db, tmp_path, monkeypatch):
    import worldwatch.export as ex

    out = tmp_path / "export"
    _surprise(db, 5)

    def disk_full(*_):
        raise OSError("disk full")

    monkeypatch.setattr(ex, "_write_state", disk_full)
    with pytest.raises(OSError):
        ex.export(tmp_path / "test.db", out)
    monkeypatch.undo()
    _surprise(db, 2, start=5)
    ex.export(tmp_path / "test.db", out)
    assert [p.name for p in (out / "surprise").iterdir()] == ["batch-000001.parquet"]
    assert len(_latest(out, "surprise")) == 7
    assert json.loads((out / "_state.json").read_text())["batch"] == 1


def test_small_tables_whole_and_rolling_ones_left_out(db, tmp_path):
    db.execute("INSERT INTO health (component, ts, event) VALUES ('x', 1, 'ok')")
    db.execute("INSERT INTO raw_ring (stream_id, cell, ts, value) VALUES ('s', 'c', 1, 1.0)")
    db.commit()
    out = tmp_path / "export"
    export(tmp_path / "test.db", out)
    assert pq.read_table(out / "snapshots" / "health.parquet").num_rows == 1
    for table in ("model_state", "presence_state", "alerts", "push_log", "digests", "sources"):
        assert (out / "snapshots" / f"{table}.parquet").exists(), table
    assert not {"raw_ring", "seen", "context"} & {p.name for p in out.iterdir()}


def test_cli_export_records_its_outcome(db, tmp_path, monkeypatch):
    from worldwatch.cli import cmd_export

    monkeypatch.setenv("WW_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("WW_EXPORT_DIR", str(tmp_path / "export"))
    _surprise(db, 2)
    cmd_export(db)
    row = db.execute("SELECT event, detail FROM health WHERE component = 'export'").fetchone()
    assert row["event"] == "ok" and json.loads(row["detail"])["surprise"] == 2
