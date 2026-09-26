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


def test_weak_single_reading_needs_accumulation(db, sources):
    """Sequential evidence (ADR 0002 §C): one 1-in-60 reading is not enough,
    two in a row are (break-even ≈ p 0.018 per reading at h = 4, k = 2)."""
    src = {
        "quake": _src(sources, "quake", "physical"),
        "news": _src(sources, "news", "informational"),
    }
    _anomalous_series(db, "quake", _cellA(), k=3)
    _surprise(db, "news", _cell_near_A(), 3, NOW, q=0.992)  # p = 0.016: S ≈ 2.1 < h
    assert run_alerts(db, src, now=NOW) == []
    _surprise(db, "news", _cell_near_A(), 3, NOW - BW, q=0.992)  # S ≈ 4.3 ≥ h
    assert len(run_alerts(db, src, now=NOW)) == 1


def test_strong_single_reading_needs_no_waiting(db, sources):
    src = {
        "quake": _src(sources, "quake", "physical"),
        "news": _src(sources, "news", "informational"),
    }
    _surprise(db, "quake", _cellA(), 3, NOW, q=0.9995)  # p = 0.001: one reading suffices
    _surprise(db, "news", _cell_near_A(), 3, NOW, q=0.9995)
    assert len(run_alerts(db, src, now=NOW)) == 1


def test_evidence_decays_after_return_to_normal(db, sources):
    src = {
        "quake": _src(sources, "quake", "physical"),
        "news": _src(sources, "news", "informational"),
    }
    _surprise(db, "quake", _cellA(), 3, NOW - 4 * BW, q=0.999)
    _surprise(db, "quake", _cellA(), 3, NOW - 3 * BW, q=0.999)
    for i in (2, 1, 0):  # three normal readings drain the CUSUM (drift k = 2 each)
        _surprise(db, "quake", _cellA(), 3, NOW - i * BW, q=0.5)
    _anomalous_series(db, "news", _cell_near_A())
    assert run_alerts(db, src, now=NOW) == []


def test_stale_series_is_not_a_candidate(db, sources):
    src = {
        "quake": _src(sources, "quake", "physical"),
        "news": _src(sources, "news", "informational"),
    }
    for i in range(3):
        _surprise(db, "quake", _cellA(), 3, NOW - 5 * 3600 - i * BW, q=0.9999)  # 5 h old
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


# --- per-source policies (doc/adr/0001-per-source-alert-policy.md) -----------


def _with_policy(sources, stream_id, modality, alerts, base="usgs_seismic", status="nursery"):
    cfg = _src(sources, stream_id, modality, base=base)
    return dataclasses.replace(cfg, status=status, extra={**cfg.extra, "alerts": alerts})


def _station(i):
    # distinct fine cells, all inside one res-3 region
    return h3.cell_to_children(h3.latlng_to_cell(48.2, 16.4, 3), 8)[i]


def test_context_role_never_corroborates(db, sources):
    src = {
        "quake": _src(sources, "quake", "physical"),
        "news": _with_policy(sources, "news", "informational", {"role": "context"}),
    }
    _anomalous_series(db, "quake", _cellA())
    _anomalous_series(db, "news", _cell_near_A())
    assert run_alerts(db, src, now=NOW) == []


RAD = {"single_source": True, "min_sensors": 3, "q_tail": 1e-6, "persist_n": 2, "region_resolution": 3}


def test_single_source_alerts_when_independent_sensors_agree(db, sources):
    src = {"rad": _with_policy(sources, "rad", "physical", RAD, status="active")}
    for i in range(3):
        _anomalous_series(db, "rad", _station(i), scale=2, k=2, q=1 - 1e-8)
    (aid,) = run_alerts(db, src, now=NOW)
    ev = json.loads(db.execute("SELECT evidence FROM alerts WHERE alert_id = ?", (aid,)).fetchone()[0])
    assert len({e["cell"] for e in ev}) == 3
    assert db.execute("SELECT severity FROM alerts").fetchone()[0] > 0.99
    assert run_alerts(db, src, now=NOW) == []  # idempotent


def test_single_faulty_sensor_does_not_alert(db, sources):
    src = {"rad": _with_policy(sources, "rad", "physical", RAD, status="active")}
    _anomalous_series(db, "rad", _station(0), scale=2, k=5, q=1 - 1e-12)
    _anomalous_series(db, "rad", _station(1), scale=2, k=5, q=0.5)
    assert run_alerts(db, src, now=NOW) == []


def test_single_source_needs_the_stricter_tail(db, sources):
    src = {"rad": _with_policy(sources, "rad", "physical", RAD, status="active")}
    for i in range(3):
        _anomalous_series(db, "rad", _station(i), scale=2, k=1, q=0.99995)  # 1-in-10k: not enough
    assert run_alerts(db, src, now=NOW) == []


def test_single_source_alerts_on_first_reading_when_sensors_agree(db, sources):
    """Confirm in space before time: two stations, one reading each."""
    pol = {"single_source": True, "min_sensors": 2, "q_tail": 1e-4, "region_resolution": 3}
    src = {"rad": _with_policy(sources, "rad", "physical", pol, status="active")}
    _surprise(db, "rad", _station(0), -1, NOW, q=1 - 2e-5)
    assert run_alerts(db, src, now=NOW) == []  # one station alone
    _surprise(db, "rad", _station(1), -1, NOW, q=1 - 3e-5)
    assert len(run_alerts(db, src, now=NOW)) == 1


def test_nursery_single_source_is_capped_below_waking(db, sources):
    src = {"rad": _with_policy(sources, "rad", "physical", RAD, status="nursery")}
    for i in range(3):
        _anomalous_series(db, "rad", _station(i), scale=2, k=2, q=1 - 1e-8)
    run_alerts(db, src, now=NOW)
    assert db.execute("SELECT severity FROM alerts").fetchone()[0] <= 0.85  # priority 4


def test_every_event_alerts_from_fresh_ingestion(db, sources):
    from worldwatch.ingest.models import Observation
    from worldwatch.store import write_observations

    pol = {"every_event": True, "severity": 0.95}
    src = {"sig": _with_policy(sources, "sig", "physical", pol)}
    quake_cell = h3.latlng_to_cell(-21.3, 168.6, 3)
    write_observations(db, [Observation("sig", quake_cell, NOW - 600, 6.6)], now=NOW - 300)
    (aid,) = run_alerts(db, src, now=NOW)  # no bins, no surprise rows needed
    row = db.execute("SELECT severity, evidence FROM alerts WHERE alert_id = ?", (aid,)).fetchone()
    assert row["severity"] == 0.95
    assert json.loads(row["evidence"])[0]["kind"] == "source_alert"
    assert run_alerts(db, src, now=NOW + 300) == []  # one alert per observation
    write_observations(db, [Observation("sig", quake_cell, NOW, 5.9)], now=NOW + 400)
    assert len(run_alerts(db, src, now=NOW + 600)) == 1  # a new item alerts again
