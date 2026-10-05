"""The nursery: promotion on calibrated PITs, quarantine on drift (ADR 0006)."""

import dataclasses

import numpy as np

from worldwatch.layer0 import nursery
from worldwatch.store import upsert_source

NOW = 1791200000
DAY = 86400


def _cfg(sources, sid, tail=None, status="nursery"):
    base = sources["usgs_seismic"]
    extra = {k: v for k, v in base.extra.items() if k != "alerts"}
    if tail:
        extra["alerts"] = {"tail": tail}
    return dataclasses.replace(base, stream_id=sid, status=status, extra=extra)


def _register(db, cfg, status=None):
    upsert_source(db, cfg.stream_id, {"class": cfg.class_, "modality": cfg.modality,
                                      "flavor": cfg.flavor, "status": status or cfg.status})


def _pits(db, sid, q, days=10, end=NOW):
    """PITs q spread evenly over the `days` before `end`, one cell."""
    ts = np.linspace(end - days * DAY, end - 60, len(q)).astype(int)
    db.executemany(
        "INSERT INTO surprise (stream_id, cell, scale, bin_start, q_value, presence_q, precision, "
        "n_obs, tail_index, model_version) VALUES (?, 'c', -1, ?, ?, 1.0, 1.0, 1, NULL, 1)",
        [(sid, int(t), float(v)) for t, v in zip(ts, q, strict=True)])
    db.commit()


def _status(db, sid):
    return db.execute("SELECT status FROM sources WHERE stream_id = ?", (sid,)).fetchone()[0]


def test_calibrated_stream_is_promoted(db, sources):
    rng = np.random.default_rng(1)
    cfg = _cfg(sources, "good")
    _register(db, cfg)
    _pits(db, "good", rng.uniform(size=5000))
    changes = nursery.run_nursery(db, {"good": cfg}, now=NOW)
    assert changes["promoted"] == 1 and _status(db, "good") == "active"
    (verdict,) = nursery.latest(db)
    assert verdict["status"] == "active" and verdict["verdict"].startswith("calibrated")
    assert db.execute("SELECT COUNT(*) FROM health WHERE component='good' AND event='promoted'").fetchone()[0] == 1


def test_overconfident_model_stays_in_the_nursery(db, sources):
    """Heavy-tailed data under a too-narrow model: PITs pile at 0 and 1."""
    rng = np.random.default_rng(2)
    cfg = _cfg(sources, "narrow")
    _register(db, cfg)
    from scipy import stats
    x = rng.standard_t(df=2, size=5000)
    _pits(db, "narrow", stats.norm.cdf(x))  # a Student-t(2) world, a Gaussian model
    nursery.run_nursery(db, {"narrow": cfg}, now=NOW)
    assert _status(db, "narrow") == "nursery"
    assert "overconfident" in nursery.latest(db)[0]["verdict"]


def test_underconfident_model_stays_in_the_nursery(db, sources):
    """A model far too wide: PITs bunch in the middle, it could never fire."""
    rng = np.random.default_rng(3)
    cfg = _cfg(sources, "wide")
    _register(db, cfg)
    from scipy import stats
    _pits(db, "wide", stats.norm.cdf(rng.normal(scale=0.3, size=5000)))
    nursery.run_nursery(db, {"wide": cfg}, now=NOW)
    assert _status(db, "wide") == "nursery"
    assert "shape off" in nursery.latest(db)[0]["verdict"]


def test_only_the_tail_that_alerts_is_judged(db, sources):
    """A quiet lower tail is no fault for a stream that alerts only on rises."""
    rng = np.random.default_rng(4)
    q = rng.uniform(size=5000)
    q[q < 0.01] += 0.02  # the lower 1% emptied into the next bin
    up, both = _cfg(sources, "up", tail="upper"), _cfg(sources, "both")
    for c in (up, both):
        _register(db, c)
        _pits(db, c.stream_id, q)
    nursery.run_nursery(db, {"up": up, "both": both}, now=NOW)
    assert _status(db, "up") == "active"
    assert _status(db, "both") == "nursery"


def test_too_little_evidence_waits(db, sources):
    rng = np.random.default_rng(5)
    few, short = _cfg(sources, "few"), _cfg(sources, "short")
    for c in (few, short):
        _register(db, c)
    _pits(db, "few", rng.uniform(size=150))
    _pits(db, "short", rng.uniform(size=5000), days=2)
    nursery.run_nursery(db, {"few": few, "short": short}, now=NOW)
    assert _status(db, "few") == _status(db, "short") == "nursery"
    assert all("not enough evidence" in v["verdict"] for v in nursery.latest(db))


def test_drift_quarantines_and_recovery_releases(db, sources):
    rng = np.random.default_rng(6)
    cfg = _cfg(sources, "drifty")
    _register(db, cfg, status="active")
    # the last 7 days: a third of the PITs stuck at the top (a broken feed, a regime change)
    q = rng.uniform(size=3000)
    q[::3] = 0.9995
    _pits(db, "drifty", q, days=6)
    nursery.run_nursery(db, {"drifty": cfg}, now=NOW)
    assert _status(db, "drifty") == "quarantined"
    # a week later, calibrated again
    _pits(db, "drifty", rng.uniform(size=3000), days=6, end=NOW + 8 * DAY)
    changes = nursery.run_nursery(db, {"drifty": cfg}, now=NOW + 8 * DAY)
    assert changes["released"] == 1 and _status(db, "drifty") == "active"


def test_active_stream_is_not_quarantined_for_a_quiet_tail(db, sources):
    rng = np.random.default_rng(7)
    cfg = _cfg(sources, "quiet")
    _register(db, cfg, status="active")
    q = rng.uniform(size=3000)
    q[q > 0.99] -= 0.02
    _pits(db, "quiet", q, days=6)
    nursery.run_nursery(db, {"quiet": cfg}, now=NOW)
    assert _status(db, "quiet") == "active"


def test_retired_in_the_stanza_is_final(db, sources):
    rng = np.random.default_rng(8)
    cfg = _cfg(sources, "gone", status="retired")
    _register(db, cfg, status="active")
    _pits(db, "gone", rng.uniform(size=5000))
    nursery.run_nursery(db, {"gone": cfg}, now=NOW)
    assert _status(db, "gone") == "retired"
    assert nursery.statuses(db, {"gone": cfg}) == {"gone": "retired"}


def test_rerun_is_idempotent(db, sources):
    rng = np.random.default_rng(9)
    cfg = _cfg(sources, "good")
    _register(db, cfg)
    _pits(db, "good", rng.uniform(size=5000))
    nursery.run_nursery(db, {"good": cfg}, now=NOW)
    again = nursery.run_nursery(db, {"good": cfg}, now=NOW)
    assert again["promoted"] == 0 and _status(db, "good") == "active"
