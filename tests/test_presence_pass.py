"""Presence pass: health table → learned silence → archived silence rows."""

import dataclasses

from worldwatch.layer0.presence import PRESENCE_CELL, run_presence

HOUR = 3600
T0 = 1_699_999_200  # hour-aligned (divisible by 3600) so slots match bin_start


def _cfg(sources, **kw):
    return dataclasses.replace(sources["usgs_seismic"], cadence_seconds=HOUR, **kw)


def _slot(day: int, hour: int) -> int:
    return T0 + (day * 24 + hour) * HOUR


def _health(db, stream, ts, event):
    db.execute(
        "INSERT OR IGNORE INTO health (component, ts, event, detail) VALUES (?, ?, ?, ?)",
        (stream, ts, event, None),
    )
    db.commit()


def _silence_rows(db, stream):
    return db.execute(
        "SELECT bin_start, presence_q, q_value, n_obs, cell FROM surprise "
        "WHERE stream_id = ? ORDER BY bin_start",
        (stream,),
    ).fetchall()


def test_healthy_source_writes_no_silence(db, sources):
    cfg = _cfg(sources)
    src = {cfg.stream_id: cfg}
    for i in range(100):
        _health(db, cfg.stream_id, _slot(0, 0) + i * HOUR + 5, "ok")
    written = run_presence(db, src, now=_slot(0, 0) + 100 * HOUR)
    assert written == 0
    assert _silence_rows(db, cfg.stream_id) == []
    # model + cursor persisted
    assert db.execute("SELECT COUNT(*) FROM presence_state").fetchone()[0] == 1


def test_outage_of_reliable_source_is_flagged(db, sources):
    cfg = _cfg(sources)
    src = {cfg.stream_id: cfg}
    # 100 reliable slots, then a 3-slot outage (poll ran, only errors)
    for i in range(100):
        _health(db, cfg.stream_id, T0 + i * HOUR + 5, "ok")
    for i in range(100, 103):
        _health(db, cfg.stream_id, T0 + i * HOUR + 5, "http_error")

    written = run_presence(db, src, now=T0 + 103 * HOUR)
    rows = _silence_rows(db, cfg.stream_id)
    assert written == len(rows) >= 1
    for r in rows:
        assert r["cell"] == PRESENCE_CELL
        assert r["q_value"] is None  # no observation
        assert r["n_obs"] == 0
        assert r["presence_q"] >= 0.5
    # surprise grows across the run
    assert rows[-1]["presence_q"] > rows[0]["presence_q"]


def test_scheduler_gap_is_not_blamed_on_source(db, sources):
    """Slots with no health events at all (poller didn't run) are skipped —
    system-deaf is not world-silent (P9)."""
    cfg = _cfg(sources)
    src = {cfg.stream_id: cfg}
    for i in range(50):
        _health(db, cfg.stream_id, T0 + i * HOUR + 5, "ok")
    # slots 50..59: nothing recorded (scheduler down)
    for i in range(60, 70):
        _health(db, cfg.stream_id, T0 + i * HOUR + 5, "ok")

    written = run_presence(db, src, now=T0 + 70 * HOUR)
    assert written == 0  # the gap produced no silence rows


def test_not_modified_counts_as_present(db, sources):
    cfg = _cfg(sources)
    src = {cfg.stream_id: cfg}
    for i in range(30):
        _health(db, cfg.stream_id, T0 + i * HOUR + 5, "not_modified")
    assert run_presence(db, src, now=T0 + 30 * HOUR) == 0


def test_learned_nightly_gap_not_flagged_but_day_outage_is(db, sources):
    """End-to-end headline: a source silent every night (learned) is not
    flagged, but a novel daytime outage is."""
    cfg = _cfg(sources)
    src = {cfg.stream_id: cfg}

    def is_day(hour: int) -> bool:
        return 6 <= hour < 22

    for day in range(40):
        for hour in range(24):
            ev = "ok" if is_day(hour) else "http_error"
            _health(db, cfg.stream_id, _slot(day, hour) + 5, ev)
    run_presence(db, src, now=_slot(40, 0))
    baseline = len(_silence_rows(db, cfg.stream_id))

    # day 40: normal night silence + a novel daytime outage at hour 10
    for hour in range(24):
        ev = "ok" if is_day(hour) else "http_error"
        if hour == 10:
            ev = "http_error"  # novel daytime outage
        _health(db, cfg.stream_id, _slot(40, hour) + 5, ev)
    run_presence(db, src, now=_slot(41, 0))

    new_rows = _silence_rows(db, cfg.stream_id)[baseline:]
    flagged_slots = {r["bin_start"] for r in new_rows}
    assert _slot(40, 10) in flagged_slots, "daytime outage should be flagged"
    for hour in list(range(0, 6)) + list(range(22, 24)):
        assert _slot(40, hour) not in flagged_slots, f"learned night gap flagged at h={hour}"


def test_incremental_cursor_advances(db, sources):
    cfg = _cfg(sources)
    src = {cfg.stream_id: cfg}
    for i in range(20):
        _health(db, cfg.stream_id, T0 + i * HOUR + 5, "ok")
    run_presence(db, src, now=T0 + 20 * HOUR)
    first_cursor = db.execute(
        "SELECT last_slot FROM presence_state WHERE stream_id = ?", (cfg.stream_id,)
    ).fetchone()["last_slot"]

    for i in range(20, 30):
        _health(db, cfg.stream_id, T0 + i * HOUR + 5, "ok")
    run_presence(db, src, now=T0 + 30 * HOUR)
    second_cursor = db.execute(
        "SELECT last_slot FROM presence_state WHERE stream_id = ?", (cfg.stream_id,)
    ).fetchone()["last_slot"]

    assert second_cursor > first_cursor


def test_retired_source_skipped(db, sources):
    cfg = _cfg(sources, status="retired")
    src = {cfg.stream_id: cfg}
    for i in range(10):
        _health(db, cfg.stream_id, T0 + i * HOUR + 5, "http_error")
    assert run_presence(db, src, now=T0 + 10 * HOUR) == 0


def _multi(sources, n):
    base = _cfg(sources)
    return {
        f"s{i}": dataclasses.replace(base, stream_id=f"s{i}", cadence_seconds=HOUR)
        for i in range(n)
    }


def test_common_mode_outage_is_suppressed(db, sources):
    """All sources down in the same windows = our internet/host, not the world."""
    src = _multi(sources, 4)
    for sid in src:
        for i in range(50):
            _health(db, sid, T0 + i * HOUR + 5, "ok")
        for i in range(50, 53):  # every source fails the same 3 windows
            _health(db, sid, T0 + i * HOUR + 5, "http_error")

    written = run_presence(db, src, now=T0 + 53 * HOUR)
    assert written == 0, "global simultaneous silence should be suppressed"
    faults = db.execute(
        "SELECT COUNT(*) FROM health WHERE component='presence' AND event='common_mode_fault'"
    ).fetchone()[0]
    assert faults == 3  # the three all-down windows tagged as our-side


def test_regional_silence_still_flagged(db, sources):
    """One source down while the others report = a real (world) signal."""
    src = _multi(sources, 4)
    for sid in src:
        for i in range(50):
            _health(db, sid, T0 + i * HOUR + 5, "ok")
    # window 50: only s0 fails, s1..s3 fine → fraction 1/4 < 0.5, not common-mode
    _health(db, "s0", T0 + 50 * HOUR + 5, "http_error")
    for sid in ("s1", "s2", "s3"):
        _health(db, sid, T0 + 50 * HOUR + 5, "ok")

    written = run_presence(db, src, now=T0 + 51 * HOUR)
    assert written >= 1
    rows = _silence_rows(db, "s0")
    assert len(rows) == 1 and rows[0]["bin_start"] == T0 + 50 * HOUR
    for sid in ("s1", "s2", "s3"):
        assert _silence_rows(db, sid) == []
    faults = db.execute("SELECT COUNT(*) FROM health WHERE event='common_mode_fault'").fetchone()[0]
    assert faults == 0
