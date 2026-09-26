"""Consolidator: fold correctness, idempotency, commutativity, fine window."""

import pytest

from worldwatch.cascade.bins import bin_for
from worldwatch.cascade.consolidator import consolidate
from worldwatch.cascade.tdigest import TDigest
from worldwatch.ingest.models import Observation
from worldwatch.store import write_observations

NOW = 2_000_000_000


def _obs(stream, cell, ts, value):
    return Observation(stream_id=stream, cell=cell, ts=ts, value=value)


def _all_bins(db):
    return db.execute(
        "SELECT stream_id, cell, scale, bin_start, n, vmin, vmax, vmean, m2, sketch "
        "FROM bins ORDER BY stream_id, cell, scale, bin_start"
    ).fetchall()


def _snapshot(db):
    """Comparable snapshot of bins (sketch blob excluded — compared separately)."""
    return [
        (
            r["stream_id"],
            r["cell"],
            r["scale"],
            r["bin_start"],
            r["n"],
            r["vmin"],
            r["vmax"],
            round(r["vmean"], 9) if r["vmean"] is not None else None,
            round(r["m2"], 6) if r["m2"] is not None else None,
        )
        for r in _all_bins(db)
    ]


def test_folds_aged_rows_and_prunes_raw(db):
    # three old obs in the same fine bin (ages > window)
    old_ts = NOW - 10 * 24 * 3600
    write_observations(
        db,
        [
            _obs("s", "cellA", old_ts + 10, 1.0),
            _obs("s", "cellA", old_ts + 20, 3.0),
            _obs("s", "cellA", old_ts + 30, 5.0),
        ],
    )
    n = consolidate(db, now=NOW)
    assert n == 3
    # raw_ring pruned
    assert db.execute("SELECT COUNT(*) FROM raw_ring").fetchone()[0] == 0
    # one bin with the three folded values
    bins = _all_bins(db)
    assert len(bins) == 1
    b = bins[0]
    assert b["n"] == 3
    assert b["vmin"] == 1.0 and b["vmax"] == 5.0
    assert b["vmean"] == pytest.approx(3.0)
    scale, bstart = bin_for(old_ts + 10, NOW)
    assert b["scale"] == scale


def test_fine_window_rows_untouched(db):
    recent_ts = NOW - 60  # inside the 48h fine window
    old_ts = NOW - 10 * 24 * 3600
    write_observations(
        db,
        [
            _obs("s", "c", recent_ts, 2.0),
            _obs("s", "c", old_ts, 9.0),
        ],
    )
    consolidate(db, now=NOW)
    # recent row remains in raw_ring; old row folded
    remaining = db.execute("SELECT ts FROM raw_ring").fetchall()
    assert [r["ts"] for r in remaining] == [recent_ts]
    assert db.execute("SELECT COUNT(*) FROM bins").fetchone()[0] == 1


def test_idempotent_rerun(db):
    old_ts = NOW - 10 * 24 * 3600
    write_observations(db, [_obs("s", "c", old_ts + i, float(i)) for i in range(20)])
    consolidate(db, now=NOW)
    snap1 = _snapshot(db)
    # re-run over same window: nothing left to fold, bins unchanged
    n2 = consolidate(db, now=NOW)
    assert n2 == 0
    assert _snapshot(db) == snap1


def test_cross_run_merge_equals_single_run(db, tmp_path):
    """Folding in two batches (different consolidation runs) equals folding once."""
    from worldwatch.db import open_db

    old = NOW - 10 * 24 * 3600
    batch1 = [_obs("s", "c", old + i, float(i)) for i in range(10)]
    batch2 = [_obs("s", "c", old + 100 + i, float(i + 10)) for i in range(10)]

    # Two-run DB: fold batch1, then insert batch2 and fold again.
    write_observations(db, batch1)
    consolidate(db, now=NOW)
    write_observations(db, batch2)
    consolidate(db, now=NOW)
    two_run = _snapshot(db)

    # Single-run DB: all at once.
    db2 = open_db(tmp_path / "single.db")
    write_observations(db2, batch1 + batch2)
    consolidate(db2, now=NOW)
    one_run = _snapshot(db2)

    assert two_run == one_run


def test_commutative_insert_order(db, tmp_path):
    from worldwatch.db import open_db

    old = NOW - 10 * 24 * 3600
    obs = [_obs("s", "c", old + i, float(i * i % 7)) for i in range(30)]

    write_observations(db, obs)
    consolidate(db, now=NOW)
    forward = _snapshot(db)

    db2 = open_db(tmp_path / "rev.db")
    write_observations(db2, list(reversed(obs)))
    consolidate(db2, now=NOW)
    backward = _snapshot(db2)

    assert forward == backward


def test_pure_event_bin_counts_without_value_stats(db):
    """Rows with NULL value (pure events) count toward n; value stats stay NULL."""
    old_ts = NOW - 10 * 24 * 3600
    write_observations(
        db,
        [
            _obs("news", "cX", old_ts + 10, None),
            _obs("news", "cX", old_ts + 20, None),
        ],
    )
    consolidate(db, now=NOW)
    b = _all_bins(db)[0]
    assert b["n"] == 2
    assert b["vmean"] is None
    assert b["vmin"] is None
    assert b["sketch"] is None


def test_sketch_is_queryable_after_fold(db):
    old = NOW - 10 * 24 * 3600
    write_observations(db, [_obs("s", "c", old + i, float(i)) for i in range(100)])
    consolidate(db, now=NOW)
    b = _all_bins(db)[0]
    digest = TDigest.from_bytes(b["sketch"])
    assert digest.quantile(0.5) == pytest.approx(49.5, abs=5.0)


# --- re-fetch after consolidation (the `seen` table) --------------------------


def test_refetch_after_consolidation_is_not_recounted(db):
    # A poller re-requests overlapping history (USGS: the last hour; Cloudflare:
    # 7 days). Once the first copy has been folded out of raw_ring, the repeat
    # must not land in the bins a second time.
    t0 = 1_000_000
    quakes = [Observation("usgs_seismic", "c", t0 + i, 4.0 + i / 10) for i in range(5)]
    assert write_observations(db, quakes, now=t0 + 10) == 5
    consolidate(db, now=t0 + 3600, fine_window_seconds=900)
    snapshot = db.execute("SELECT * FROM bins ORDER BY scale, bin_start").fetchall()
    assert sum(r["n"] for r in snapshot) == 5

    for k in range(1, 4):  # three more polls return the same quakes
        assert write_observations(db, quakes, now=t0 + 3600 * k) == 0
        consolidate(db, now=t0 + 3600 * (k + 1), fine_window_seconds=900)

    after = db.execute("SELECT * FROM bins ORDER BY scale, bin_start").fetchall()
    assert sum(r["n"] for r in after) == 5
    assert db.execute("SELECT COUNT(*) FROM raw_ring").fetchone()[0] == 0


def test_new_observations_among_refetched_ones_still_land(db):
    t0 = 1_000_000
    first = [Observation("s", "c", t0 + i, 1.0) for i in range(3)]
    write_observations(db, first, now=t0)
    consolidate(db, now=t0 + 3600, fine_window_seconds=900)
    batch = first + [Observation("s", "c", t0 + 100, 2.0)]
    assert write_observations(db, batch, now=t0 + 3700) == 1


def test_seen_keys_pruned_after_retention(db):
    t0 = 1_000_000
    write_observations(db, [Observation("s", "c", t0, 1.0)], now=t0)
    consolidate(db, now=t0 + 3600, fine_window_seconds=900, seen_retention_seconds=86400)
    assert db.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 1
    consolidate(db, now=t0 + 86400 + 1, fine_window_seconds=900, seen_retention_seconds=86400)
    assert db.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0


def test_seen_pruned_by_first_seen_not_observation_time(db):
    # A feed returning old-dated rows (Safecast measurements from years ago)
    # is still recognised while its first sighting is recent.
    now = 2_000_000_000
    old = Observation("safecast_radiation", "c", now - 5 * 365 * 86400, 38.0)
    assert write_observations(db, [old], now=now) == 1
    consolidate(db, now=now + 3600, fine_window_seconds=900)
    assert write_observations(db, [old], now=now + 7200) == 0


def test_migration_backfills_seen_from_raw_ring(tmp_path):
    import sqlite3

    from worldwatch.db import MIGRATIONS, open_db

    path = tmp_path / "v3.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)")
    for i, sql in enumerate(MIGRATIONS[:3], start=1):
        conn.executescript(sql)
        conn.execute("INSERT INTO schema_version VALUES (?, 0)", (i,))
    conn.execute("INSERT INTO raw_ring VALUES ('s', 'c', 123, 1.0, NULL)")
    conn.commit()
    conn.close()

    db = open_db(path)  # applies v4
    assert [tuple(r) for r in db.execute("SELECT stream_id, cell, ts FROM seen")] == [("s", "c", 123)]
    assert write_observations(db, [Observation("s", "c", 123, 1.0)]) == 0
