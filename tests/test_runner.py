"""Layer-0 runner: end-to-end wiring, idempotency, warm restart, gating."""

import dataclasses

from worldwatch.cascade.bins import bin_width
from worldwatch.cascade.consolidator import consolidate
from worldwatch.ingest.models import Observation
from worldwatch.layer0.runner import run_layer0
from worldwatch.store import write_observations

NOW = 2_000_000_000


def _insert_bin(db, stream, cell, scale, bin_start, n, vmean):
    db.execute(
        "INSERT INTO bins (stream_id, cell, scale, bin_start, n, vmin, vmax, vmean, m2) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (stream, cell, scale, bin_start, n, vmean, vmean, vmean, 0.0),
    )
    db.commit()


def _surprise_rows(db, stream=None):
    q = "SELECT * FROM surprise"
    args = ()
    if stream:
        q += " WHERE stream_id = ?"
        args = (stream,)
    return db.execute(q + " ORDER BY cell, scale, bin_start", args).fetchall()


def test_end_to_end_consolidate_then_score(db, sources):
    """raw_ring → consolidate → run_layer0 → surprise, for both flavors."""
    # continuous stream (btc_usd, cell GLOBAL)
    btc = sources["btc_usd"]
    write_observations(
        db,
        [Observation("btc_usd", "GLOBAL", NOW - 400 - i * 300, 100.0 + (i % 5)) for i in range(60)],
    )
    # count stream (usgs_seismic, one cell, several events per bin)
    write_observations(
        db,
        [
            Observation("usgs_seismic", "cellA", NOW - 400 - i * 120, 2.0 + (i % 3))
            for i in range(60)
        ],
    )
    consolidate(db, now=NOW, fine_window_seconds=300)
    assert db.execute("SELECT COUNT(*) FROM bins").fetchone()[0] > 0

    written = run_layer0(db, sources, now=NOW)
    assert written > 0

    rows = _surprise_rows(db)
    assert len(rows) == written
    for r in rows:
        assert 0.0 <= r["q_value"] <= 1.0
        assert r["presence_q"] == 1.0
        assert r["n_obs"] >= 1
        assert r["model_version"] == 1

    # model_state persisted, one row per (cell, scale) group scored
    groups = db.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT stream_id, cell, scale FROM surprise)"
    ).fetchone()[0]
    state_rows = db.execute("SELECT COUNT(*) FROM model_state").fetchone()[0]
    assert state_rows == groups
    # self-instrumented
    assert db.execute("SELECT COUNT(*) FROM health WHERE component='layer0'").fetchone()[0] == 1
    assert btc.flavor == "continuous"


def test_rerun_is_idempotent(db, sources):
    write_observations(
        db,
        [Observation("btc_usd", "GLOBAL", NOW - 400 - i * 300, 50.0) for i in range(40)],
    )
    consolidate(db, now=NOW, fine_window_seconds=300)
    first = run_layer0(db, sources, now=NOW)
    assert first > 0
    before = _surprise_rows(db)

    second = run_layer0(db, sources, now=NOW + 1)
    assert second == 0  # nothing new to score
    assert _surprise_rows(db) == before


def test_warm_restart_uses_saved_state(db, sources):
    """A model reloaded from model_state continues the series: a freshly
    appended bin is scored with a real predictive, not the cold-start 0.5."""
    w = bin_width(3)
    base = (NOW // w) * w - 20 * w
    for i in range(6):
        _insert_bin(db, "usgs_seismic", "cellW", 3, base + i * w, n=10, vmean=2.5)

    assert run_layer0(db, sources, now=NOW) == 6
    state_row = db.execute(
        "SELECT updated_at FROM model_state WHERE stream_id='usgs_seismic' AND cell='cellW'"
    ).fetchone()

    # append one more bin to the SAME (cell, scale) group and re-run
    _insert_bin(db, "usgs_seismic", "cellW", 3, base + 6 * w, n=99, vmean=6.0)
    assert run_layer0(db, sources, now=NOW + 100) == 1

    appended = db.execute(
        "SELECT q_value FROM surprise WHERE stream_id='usgs_seismic' AND cell='cellW' "
        "AND bin_start = ?",
        (base + 6 * w,),
    ).fetchone()
    # warm model scores the n=99 burst high; a cold model would have returned 0.5
    assert appended["q_value"] != 0.5
    assert appended["q_value"] > 0.9  # burst relative to the n=10 history

    new_state = db.execute(
        "SELECT updated_at FROM model_state WHERE stream_id='usgs_seismic' AND cell='cellW'"
    ).fetchone()
    assert new_state["updated_at"] > state_row["updated_at"]


def test_retired_source_is_skipped(db, sources):
    retired = {"btc_usd": dataclasses.replace(sources["btc_usd"], status="retired")}
    write_observations(
        db,
        [Observation("btc_usd", "GLOBAL", NOW - 400 - i * 300, 10.0) for i in range(20)],
    )
    consolidate(db, now=NOW, fine_window_seconds=300)
    assert run_layer0(db, retired, now=NOW) == 0
    assert db.execute("SELECT COUNT(*) FROM surprise").fetchone()[0] == 0


def test_unsupported_flavor_is_skipped(db, sources):
    rate_src = {"foo": dataclasses.replace(sources["btc_usd"], stream_id="foo", flavor="rate")}
    _insert_bin(db, "foo", "GLOBAL", 2, 1000, n=1, vmean=5.0)
    assert run_layer0(db, rate_src, now=NOW) == 0


def test_flavor_switch_resets_stale_model_state(db, sources):
    """A saved model_state from a source's previous flavor (e.g. wikipedia's
    count → continuous switch) cold-starts instead of crashing the pass, and
    the reset is recorded as health data."""
    w = bin_width(3)
    base = (NOW // w) * w - 20 * w
    count_cfg = dataclasses.replace(sources["wikipedia_pageviews"], flavor="count")
    for i in range(3):
        _insert_bin(db, "wikipedia_pageviews", "en.wikipedia.org", 3, base + i * w, 5, 15.9)
    assert run_layer0(db, {"wikipedia_pageviews": count_cfg}, now=NOW) == 3
    # model_state now holds a count-model blob for this group

    _insert_bin(db, "wikipedia_pageviews", "en.wikipedia.org", 3, base + 3 * w, 5, 15.9)
    written = run_layer0(db, sources, now=NOW + 100)  # real config: continuous

    assert written == 1  # scored (cold-started), not crashed
    reset = db.execute(
        "SELECT detail FROM health WHERE component='wikipedia_pageviews' AND event='model_reset'"
    ).fetchone()
    assert reset is not None and "en.wikipedia.org/3" in reset["detail"]
    # the replaced state now loads cleanly as the new flavor → no reset next run
    _insert_bin(db, "wikipedia_pageviews", "en.wikipedia.org", 3, base + 4 * w, 5, 15.9)
    assert run_layer0(db, sources, now=NOW + 200) == 1
    resets = db.execute("SELECT COUNT(*) FROM health WHERE event='model_reset'").fetchone()[0]
    assert resets == 1


def test_first_bin_in_group_is_cold_start(db, sources):
    _insert_bin(db, "btc_usd", "GLOBAL", 2, 1000, n=1, vmean=42.0)
    run_layer0(db, sources, now=NOW)
    row = db.execute("SELECT q_value FROM surprise WHERE stream_id='btc_usd'").fetchone()
    assert row["q_value"] == 0.5  # no predictive on the first observation
