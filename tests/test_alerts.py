"""Naive alert engine: persistence × geographic coherence × corroboration."""

import dataclasses
import json

import h3

from worldwatch.alerts.engine import run_alerts
from worldwatch.layer0.presence import PRESENCE_CELL

NOW = 2_000_000_000
BW = 300  # a bin width for spacing anomalous bins


def _src(sources, stream_id, modality, base="usgs_seismic"):
    return dataclasses.replace(
        sources[base], stream_id=stream_id, modality=modality, status="nursery"
    )


def _surprise(db, stream, cell, scale, bin_start, q=None, presence_q=1.0, n=1):
    db.execute(
        "INSERT OR REPLACE INTO surprise (stream_id, cell, scale, bin_start, q_value, "
        "presence_q, precision, n_obs, tail_index, model_version) "
        "VALUES (?, ?, ?, ?, ?, ?, 1.0, ?, NULL, 1)",
        (stream, cell, scale, bin_start, q, presence_q, n),
    )
    db.commit()


def _anomalous_series(db, stream, cell, scale=3, k=3, q=0.999):
    """k consecutive anomalous bins ending at NOW."""
    for i in range(k):
        _surprise(db, stream, cell, scale, NOW - (k - 1 - i) * BW, q=q)


def _cellA():
    return h3.latlng_to_cell(38.10, -122.50, 3)


def _cell_near_A():
    return h3.latlng_to_cell(38.20, -122.40, 3)  # shares a coarse (res-2) parent


def _cell_far():
    return h3.latlng_to_cell(-33.9, 151.2, 3)  # Sydney — different region


def test_single_stream_spike_does_not_escalate(db, sources):
    src = {"quake": _src(sources, "quake", "physical")}
    _anomalous_series(db, "quake", _cellA())
    assert run_alerts(db, src, now=NOW) == []
    assert db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


def test_two_modalities_same_region_corroborate(db, sources):
    src = {
        "quake": _src(sources, "quake", "physical"),
        "news": _src(sources, "news", "informational"),
    }
    _anomalous_series(db, "quake", _cellA())
    _anomalous_series(db, "news", _cell_near_A())  # nearby → same coarse region

    created = run_alerts(db, src, now=NOW)
    assert len(created) == 1
    row = db.execute("SELECT severity, cell, evidence FROM alerts").fetchone()
    assert row["severity"] > 0
    evidence = json.loads(row["evidence"])
    assert {e["modality"] for e in evidence} == {"physical", "informational"}


def test_persistence_required(db, sources):
    src = {
        "quake": _src(sources, "quake", "physical"),
        "news": _src(sources, "news", "informational"),
    }
    _anomalous_series(db, "quake", _cellA(), k=3)
    # news anomalous only once (< persist_n) → not persistent → no corroboration
    _surprise(db, "news", _cell_near_A(), 3, NOW, q=0.999)
    assert run_alerts(db, src, now=NOW, persist_n=2) == []


def test_latest_bin_must_be_anomalous(db, sources):
    src = {
        "quake": _src(sources, "quake", "physical"),
        "news": _src(sources, "news", "informational"),
    }
    # quake: two old anomalies then a normal latest bin → not currently anomalous
    _surprise(db, "quake", _cellA(), 3, NOW - 2 * BW, q=0.999)
    _surprise(db, "quake", _cellA(), 3, NOW - BW, q=0.999)
    _surprise(db, "quake", _cellA(), 3, NOW, q=0.5)  # back to normal
    _anomalous_series(db, "news", _cell_near_A())
    assert run_alerts(db, src, now=NOW) == []


def test_far_apart_anomalies_do_not_corroborate(db, sources):
    src = {
        "quake": _src(sources, "quake", "physical"),
        "news": _src(sources, "news", "informational"),
    }
    _anomalous_series(db, "quake", _cellA())
    _anomalous_series(db, "news", _cell_far())  # different coarse region
    assert run_alerts(db, src, now=NOW) == []


def test_lower_tail_is_anomalous(db, sources):
    """A crash (q near 0) is as anomalous as a spike (q near 1)."""
    src = {
        "quake": _src(sources, "quake", "physical"),
        "mkt": _src(sources, "mkt", "economic"),
    }
    _anomalous_series(db, "quake", _cellA(), q=0.999)
    _anomalous_series(db, "mkt", _cell_near_A(), q=0.001)  # lower tail
    assert len(run_alerts(db, src, now=NOW)) == 1


def test_data_row_presence_placeholder_not_flagged(db, sources):
    """Data rows carry presence_q=1.0 (runner placeholder); that must NOT be
    read as a silence anomaly."""
    src = {
        "quake": _src(sources, "quake", "physical"),
        "news": _src(sources, "news", "informational"),
    }
    # normal q_values, placeholder presence_q=1.0
    for i in range(3):
        _surprise(db, "quake", _cellA(), 3, NOW - i * BW, q=0.5, presence_q=1.0)
        _surprise(db, "news", _cell_near_A(), 3, NOW - i * BW, q=0.5, presence_q=1.0)
    assert run_alerts(db, src, now=NOW) == []


def test_multisource_silence_alerts(db, sources):
    """Two distinct-modality sources persistently silent → silence alarm."""
    src = {
        "quake": _src(sources, "quake", "physical"),
        "news": _src(sources, "news", "informational"),
    }
    for i in range(3):
        _surprise(db, "quake", PRESENCE_CELL, 0, NOW - i * BW, q=None, presence_q=0.98)
        _surprise(db, "news", PRESENCE_CELL, 0, NOW - i * BW, q=None, presence_q=0.98)

    created = run_alerts(db, src, now=NOW)
    assert len(created) == 1
    assert db.execute("SELECT cell FROM alerts").fetchone()["cell"] == PRESENCE_CELL


def test_rerun_does_not_duplicate(db, sources):
    src = {
        "quake": _src(sources, "quake", "physical"),
        "news": _src(sources, "news", "informational"),
    }
    _anomalous_series(db, "quake", _cellA())
    _anomalous_series(db, "news", _cell_near_A())

    assert len(run_alerts(db, src, now=NOW)) == 1
    assert run_alerts(db, src, now=NOW + 1) == []  # open alert already exists
    assert db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1


def test_retired_source_excluded(db, sources):
    src = {
        "quake": _src(sources, "quake", "physical"),
        "news": dataclasses.replace(_src(sources, "news", "informational"), status="retired"),
    }
    _anomalous_series(db, "quake", _cellA())
    _anomalous_series(db, "news", _cell_near_A())
    assert run_alerts(db, src, now=NOW) == []  # retired news can't corroborate
