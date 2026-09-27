"""Naive alert engine: persistence × geographic coherence × corroboration."""

import dataclasses
import json

import h3

from worldwatch.alerts.engine import run_alerts
from worldwatch.layer0.presence import PRESENCE_CELL

NOW = 2_000_000_000
BW = 300  # a bin width for spacing anomalous bins


def _src(sources, stream_id, modality, base="usgs_seismic"):
    """A synthetic stream copied from a real stanza, minus its alert policy
    (the base's [alerts] table must not leak into the test's streams)."""
    base_cfg = sources[base]
    extra = {k: v for k, v in base_cfg.extra.items() if k != "alerts"}
    return dataclasses.replace(
        base_cfg, stream_id=stream_id, modality=modality, status="nursery", extra=extra
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


# --- floods (2026-09-27: the prober re-alerted each round, and in 43 countries at once) --------

PROBE = {"single_source": True, "min_sensors": 1, "q_tail": 1e-4, "region_resolution": 3}


def test_a_persisting_single_source_anomaly_opens_one_alert_per_episode(db, sources):
    src = {"probe": _with_policy(sources, "probe", "infrastructural", PROBE, status="active")}
    cell = h3.latlng_to_cell(48.2, 16.4, 3)
    opened = []
    for i in range(30):  # a 2-minute round for an hour: each round re-scored, the window moving on
        t = NOW + 120 * i
        _surprise(db, "probe", cell, -1, t, q=1 - 1e-6)
        opened += run_alerts(db, src, now=t)
    assert len(opened) == 1
    later = NOW + 7 * 3600  # past the 6-hour cooldown: a new episode may alert
    _surprise(db, "probe", cell, -1, later, q=1 - 1e-6)
    assert len(run_alerts(db, src, now=later)) == 1


def test_failures_everywhere_at_once_are_our_vantage_point_not_alerts(db, sources):
    src = {"probe": _with_policy(sources, "probe", "infrastructural", {**PROBE, "max_regions": 3},
                                 status="active")}
    places = [(48.2, 16.4), (40.4, -3.7), (52.5, 13.4), (35.7, 139.7), (-33.9, 151.2)]
    for lat, lng in places:
        _surprise(db, "probe", h3.latlng_to_cell(lat, lng, 3), -1, NOW, q=1 - 1e-6)
    assert run_alerts(db, src, now=NOW) == []
    assert db.execute("SELECT detail FROM health WHERE component = 'probe' AND event = 'vantage_suspect'"
                      ).fetchone()[0] == "regions=5"
    # a few at once are still real
    db.execute("DELETE FROM surprise")
    for lat, lng in places[:2]:
        _surprise(db, "probe", h3.latlng_to_cell(lat, lng, 3), -1, NOW + 60, q=1 - 1e-6)
    assert len(run_alerts(db, src, now=NOW + 60)) == 2


def test_every_event_cooldown_holds_back_re_reports_of_one_ongoing_outage(db, sources):
    pol = {"every_event": True, "severity": 0.8, "fresh_seconds": 3600, "cooldown_seconds": 21600}
    src = {"ioda": _with_policy(sources, "ioda", "infrastructural", pol, status="active")}
    cell = h3.latlng_to_cell(48.2, 16.4, 3)
    opened = []
    for i in range(6):  # the same outage reported again every 30 minutes
        t = NOW + 1800 * i
        db.execute("INSERT INTO seen (stream_id, cell, ts, first_seen) VALUES (?, ?, ?, ?)", ("ioda", cell, t, t))
        db.commit()
        opened += run_alerts(db, src, now=t)
    assert len(opened) == 1


# --- early, unconfirmed alerts and escalation (alert at once, raise the priority as confirmation comes)


def test_one_very_strong_stream_alerts_at_once_unconfirmed(db, sources):
    src = {"quake": _src(sources, "quake", "physical"), "news": _src(sources, "news", "informational")}
    _surprise(db, "quake", _cellA(), 3, NOW, q=1 - 1e-4)  # strong, but alone: waits for corroboration
    assert run_alerts(db, src, now=NOW) == []
    _surprise(db, "quake", _cellA(), 3, NOW + BW, q=1 - 1e-11)  # p ~ 2e-11: alone, but not waiting
    created = run_alerts(db, src, now=NOW + BW)
    assert len(created) == 1
    row = db.execute("SELECT stage, severity, evidence FROM alerts").fetchone()
    assert row["stage"] == 0 and row["severity"] == 0.6
    assert json.loads(row["evidence"])[0]["kind"] == "provisional"
    assert run_alerts(db, src, now=NOW + BW) == []  # re-run: no second alert


def test_an_unconfirmed_alert_escalates_when_a_second_modality_agrees(db, sources):
    src = {"quake": _src(sources, "quake", "physical"), "net": _src(sources, "net", "infrastructural")}
    _surprise(db, "quake", _cellA(), 3, NOW, q=1 - 1e-11)
    [aid] = run_alerts(db, src, now=NOW)
    _surprise(db, "net", _cell_near_A(), 3, NOW + BW, q=1 - 1e-3)  # the same region, another modality
    assert run_alerts(db, src, now=NOW + BW) == [aid]  # the same alert, returned again: re-push
    row = db.execute("SELECT stage, severity, escalated_at, evidence FROM alerts").fetchone()
    assert row["stage"] == 2  # 2 x (10.7 + 2.7) >= 15: extreme
    assert row["severity"] == 0.95 and row["escalated_at"] == NOW + BW
    assert {e["modality"] for e in json.loads(row["evidence"])} == {"physical", "infrastructural"}
    assert db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1
    assert run_alerts(db, src, now=NOW + 2 * BW) == []  # escalated once


def test_escalation_is_gradual(db, monkeypatch, sources):
    monkeypatch.setenv("WW_PUSH_EXTREME_SCORE", "40")
    src = {"quake": _src(sources, "quake", "physical"), "net": _src(sources, "net", "infrastructural"),
           "wx": _src(sources, "wx", "environmental")}
    _surprise(db, "quake", _cellA(), 3, NOW, q=1 - 1e-11)
    [aid] = run_alerts(db, src, now=NOW)
    _surprise(db, "net", _cell_near_A(), 3, NOW + BW, q=1 - 1e-3)
    assert run_alerts(db, src, now=NOW + BW) == [aid]
    assert db.execute("SELECT stage FROM alerts").fetchone()[0] == 1  # confirmed, 2 x 13.4 < 40
    _surprise(db, "wx", _cellA(), 3, NOW + 2 * BW, q=1 - 1e-4)
    assert run_alerts(db, src, now=NOW + 2 * BW) == [aid]
    assert db.execute("SELECT stage FROM alerts").fetchone()[0] == 2  # 3 x 17.1 >= 40


def test_far_away_candidates_do_not_escalate(db, sources):
    src = {"quake": _src(sources, "quake", "physical"), "net": _src(sources, "net", "infrastructural")}
    _surprise(db, "quake", _cellA(), 3, NOW, q=1 - 1e-11)
    run_alerts(db, src, now=NOW)
    _surprise(db, "net", _cell_far(), 3, NOW + BW, q=1 - 1e-11)  # Sydney: its own unconfirmed alert
    run_alerts(db, src, now=NOW + BW)
    assert [r[0] for r in db.execute("SELECT stage FROM alerts")] == [0, 0]


def test_very_strong_readings_everywhere_are_our_vantage_point(db, sources):
    src = {"net": _src(sources, "net", "infrastructural")}
    places = [(48.2, 16.4), (40.4, -3.7), (52.5, 13.4), (35.7, 139.7), (-33.9, 151.2), (38.1, -122.5)]
    for lat, lng in places:
        _surprise(db, "net", h3.latlng_to_cell(lat, lng, 3), 3, NOW, q=1 - 1e-11)
    assert run_alerts(db, src, now=NOW) == []
    assert db.execute("SELECT detail FROM health WHERE component = 'net' AND event = 'vantage_suspect'"
                      ).fetchone()[0] == "regions=6"


def test_news_never_escalates_an_alert(db, sources):
    src = {"quake": _src(sources, "quake", "physical"),
           "news": _with_policy(sources, "news", "informational", {"role": "context"})}
    _surprise(db, "quake", _cellA(), 3, NOW, q=1 - 1e-11)
    run_alerts(db, src, now=NOW)
    _anomalous_series(db, "news", _cell_near_A(), q=1 - 1e-9)
    assert run_alerts(db, src, now=NOW + BW) == []
    assert db.execute("SELECT stage FROM alerts").fetchone()[0] == 0


# --- tail direction: for what can only harm one way, the other way is no evidence


def test_a_one_sided_stream_ignores_the_harmless_direction(db, sources):
    up = _with_policy(sources, "rad", "physical", {"tail": "upper"})
    net = _with_policy(sources, "net", "infrastructural", {})
    src = {"rad": up, "net": net}
    _anomalous_series(db, "rad", _cellA(), q=0.001)  # unusually low dose: not evidence
    _anomalous_series(db, "net", _cell_near_A())
    assert run_alerts(db, src, now=NOW) == []
    db.execute("DELETE FROM surprise")
    _anomalous_series(db, "rad", _cellA(), q=0.999)  # unusually high: evidence
    _anomalous_series(db, "net", _cell_near_A())
    assert len(run_alerts(db, src, now=NOW + BW)) == 1


def test_one_sided_tails_stay_calibrated():
    import random

    from worldwatch.alerts.engine import surprisal

    rng = random.Random(1)
    for tail in ("upper", "lower", "both"):
        m = sum(surprisal(rng.random(), tail) for _ in range(20000)) / 20000
        assert abs(m - 1.0) < 0.03  # Exp(1) under H0 either way
