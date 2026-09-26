"""Live scoring (ADR 0002 §A): score on arrival at native resolution,
exactly-once across crashes, and alerts within the same poll."""

import dataclasses
import json
import math

import h3
import httpx

from worldwatch.alerts.engine import open_alerts
from worldwatch.cascade.consolidator import consolidate
from worldwatch.ingest.models import Observation
from worldwatch.layer0.live import LiveScorer
from worldwatch.layer0.native import NATIVE_SCALE, native_seconds
from worldwatch.poll.http import CacheValidators
from worldwatch.poll.poller import poll_once
from worldwatch.store import write_new_observations

T0 = 1_999_999_800  # a multiple of 300 and 900: native windows start at T0
UK = h3.latlng_to_cell(54.0, -2.5, 2)


def _native_rows(db, sid=None):
    q = "SELECT stream_id, cell, bin_start, q_value, n_obs FROM surprise WHERE scale = ?"
    args: tuple = (NATIVE_SCALE,)
    if sid:
        q += " AND stream_id = ?"
        args += (sid,)
    return db.execute(q + " ORDER BY bin_start", args).fetchall()


def _feed(db, live, cfg, obs, now):
    new = write_new_observations(db, obs, now=now)
    live.ingest(cfg, new, now)
    return new


# --- continuous: scored on arrival -----------------------------------------------


def test_continuous_scored_immediately_at_observation_time(db, sources):
    cfg = sources["btc_usd"]
    live = LiveScorer(db, sources, now=T0)
    for i in range(5):
        _feed(db, live, cfg, [Observation("btc_usd", "GLOBAL", T0 + 60 * i, math.log1p(84000 + i))], T0 + 60 * i)
    rows = _native_rows(db, "btc_usd")
    assert [r["bin_start"] for r in rows] == [T0 + 60 * i for i in range(5)]
    assert db.execute("SELECT COUNT(*) FROM raw_ring WHERE scored = 0").fetchone()[0] == 0
    state = db.execute("SELECT COUNT(*) FROM model_state WHERE scale = ?", (NATIVE_SCALE,)).fetchone()
    assert state[0] == 1


def test_out_of_order_observation_recorded_not_scored(db, sources):
    cfg = sources["btc_usd"]
    live = LiveScorer(db, sources, now=T0)
    _feed(db, live, cfg, [Observation("btc_usd", "GLOBAL", T0 + 600, 11.3)], T0 + 600)
    _feed(db, live, cfg, [Observation("btc_usd", "GLOBAL", T0 + 300, 11.3)], T0 + 700)
    assert len(_native_rows(db, "btc_usd")) == 1
    ev = db.execute("SELECT event FROM health WHERE component = 'btc_usd'").fetchall()
    assert "out_of_order" in [r[0] for r in ev]


# --- counts: arrival windows, zeros, grace ----------------------------------------


def test_count_windows_close_after_grace_and_score_zeros(db, sources):
    cfg = sources["usgs_seismic"]
    w = native_seconds(cfg)
    cell = h3.latlng_to_cell(35.7, 139.7, 3)
    live = LiveScorer(db, sources, now=T0, grace_seconds=60)
    quakes = [Observation("usgs_seismic", cell, T0 + i, 3.0 + i / 10) for i in range(3)]
    _feed(db, live, cfg, quakes, T0 + 30)
    assert live.tick(T0 + w + 30) == 0  # window ended, but still within grace
    assert live.tick(T0 + w + 61) == 1
    live.tick(T0 + 3 * w + 61)  # two quiet windows later
    rows = _native_rows(db, "usgs_seismic")
    assert [r["n_obs"] for r in rows] == [3, 0, 0]  # zeros are observations too
    assert db.execute("SELECT COUNT(*) FROM live_windows").fetchone()[0] == 0


def test_quiet_cells_draw_independent_pit_values(db, sources):
    # the randomized PIT's u was once one shared sequence: every country with
    # zero unreachable targets got the same q in the same window, so a small
    # draw lit up the whole map at once
    cfg = sources["usgs_seismic"]
    w = native_seconds(cfg)
    cells = [h3.latlng_to_cell(lat, 10.0, 3) for lat in (40.0, 45.0, 50.0, 55.0)]
    live = LiveScorer(db, sources, now=T0, grace_seconds=60)
    live.register_cells("usgs_seismic", cells, T0)
    for k in range(1, 21):  # registered cells start at the newest closed window
        live.tick(T0 + k * w + 61)
    by_window: dict[int, set[float]] = {}
    for r in _native_rows(db, "usgs_seismic"):
        by_window.setdefault(r["bin_start"], set()).add(round(r["q_value"], 9))
    assert len(by_window) > 5
    assert all(len(qs) == len(cells) for ws, qs in by_window.items() if ws > T0)


def test_legacy_shared_seed_is_replaced_on_load(db, sources):
    from worldwatch.layer0 import models
    from worldwatch.layer0.count import LEGACY_SEED, BayesianCount

    blob = BayesianCount().to_bytes()  # a state saved before per-cell seeds
    a = models.load_model("count", blob, ("probe_reachability", "a"))
    b = models.load_model("count", blob, ("probe_reachability", "b"))
    assert a.seed != LEGACY_SEED and a.seed != b.seed
    assert models.load_model("count", a.to_bytes(), ("probe_reachability", "a")).seed == a.seed


def test_counts_bucket_by_arrival_not_origin(db, sources):
    """USGS publishes international quakes 20–40 min after origin; counting by
    arrival lets a window close right after it ends."""
    cfg = sources["usgs_seismic"]
    w = native_seconds(cfg)
    cell = h3.latlng_to_cell(-21.3, 168.6, 3)
    live = LiveScorer(db, sources, now=T0)
    _feed(db, live, cfg, [Observation("usgs_seismic", cell, T0 - 2400, 6.6)], T0 + 10)  # 40 min old
    live.tick(T0 + w + 61)
    (row,) = _native_rows(db, "usgs_seismic")
    assert row["bin_start"] == T0 and row["n_obs"] == 1


def test_inactive_cells_stop_scoring_zeros(db, sources):
    cfg = sources["usgs_seismic"]
    w = native_seconds(cfg)
    cell = h3.latlng_to_cell(35.7, 139.7, 3)
    live = LiveScorer(db, sources, now=T0, active_seconds=3 * w)
    _feed(db, live, cfg, [Observation("usgs_seismic", cell, T0, 3.0)], T0 + 1)
    live.tick(T0 + 10 * w)
    n = len(_native_rows(db, "usgs_seismic"))
    live.tick(T0 + 20 * w)
    assert len(_native_rows(db, "usgs_seismic")) == n  # cell went quiet for > active_seconds


# --- exactly-once across restarts and crashes -------------------------------------


def test_restart_continues_models_without_rescoring(db, sources):
    cfg = sources["btc_usd"]
    a = LiveScorer(db, sources, now=T0)
    _feed(db, a, cfg, [Observation("btc_usd", "GLOBAL", T0, 11.30)], T0)
    _feed(db, a, cfg, [Observation("btc_usd", "GLOBAL", T0 + 60, 11.31)], T0 + 60)
    b = LiveScorer(db, sources, now=T0 + 120)  # the process restarted
    assert b.replay(T0 + 120) == 0
    _feed(db, b, cfg, [Observation("btc_usd", "GLOBAL", T0 + 120, 11.32)], T0 + 120)
    rows = _native_rows(db, "btc_usd")
    assert len(rows) == 3 and rows[0]["q_value"] == 0.5  # only the very first is uninformative
    assert rows[2]["q_value"] != 0.5  # the restarted model kept its state


def test_replay_scores_what_a_crash_left_unscored(db, sources):
    cell = h3.latlng_to_cell(35.7, 139.7, 3)
    write_new_observations(db, [Observation("btc_usd", "GLOBAL", T0, 11.3),
                                Observation("usgs_seismic", cell, T0, 3.2)], now=T0)
    # crash here: stored, never scored
    live = LiveScorer(db, sources, now=T0 + 400)
    live.replay(T0 + 10)
    assert len(_native_rows(db, "btc_usd")) == 1
    assert db.execute("SELECT COUNT(*) FROM raw_ring WHERE scored = 0").fetchone()[0] == 0
    live.tick(T0 + native_seconds(sources["usgs_seismic"]) + 61)
    assert _native_rows(db, "usgs_seismic")[0]["n_obs"] == 1


def test_consolidator_waits_for_the_live_scorer(db, sources):
    write_new_observations(db, [Observation("btc_usd", "GLOBAL", T0, 11.3)], now=T0)
    live_streams = {"btc_usd"}
    assert consolidate(db, now=T0 + 3600, fine_window_seconds=900, live_streams=live_streams) == 0
    LiveScorer(db, sources, now=T0).replay(T0 + 3600)
    assert consolidate(db, now=T0 + 3600, fine_window_seconds=900, live_streams=live_streams) == 1


def test_evidence_restored_after_restart(db, sources):
    cfg = sources["btc_usd"]
    a = LiveScorer(db, sources, now=T0)
    for i in range(60):
        _feed(db, a, cfg, [Observation("btc_usd", "GLOBAL", T0 + 60 * i, 11.3 + 1e-4 * (i % 3))], T0 + 60 * i)
    _feed(db, a, cfg, [Observation("btc_usd", "GLOBAL", T0 + 3600, 12.3)], T0 + 3600)  # a crash
    assert [c.stream_id for c in a.candidates(T0 + 3600)] == ["btc_usd"]
    b = LiveScorer(db, sources, now=T0 + 3600)
    assert [c.stream_id for c in b.candidates(T0 + 3600)] == ["btc_usd"]


# --- the fast path: alert within the poll ------------------------------------------


def test_radiation_alerts_on_first_reading_when_two_stations_jump(db, sources):
    """Confirm in space before time (ADR 0002 §C)."""
    cfg = dataclasses.replace(sources["eurdep_gamma"], status="active")
    src = {"eurdep_gamma": cfg}
    stations = h3.cell_to_children(h3.latlng_to_cell(48.2, 16.4, 3), 8)[:2]
    live = LiveScorer(db, src, now=T0)
    for hour in range(48):
        t = T0 + 3600 * hour
        obs = [Observation("eurdep_gamma", s, t, math.log(0.10 * (1 + 0.02 * ((hour + j) % 3))))
               for j, s in enumerate(stations)]
        _feed(db, live, cfg, obs, t)
    assert open_alerts(db, src, live.candidates(T0 + 3600 * 47), T0 + 3600 * 47) == []
    t = T0 + 3600 * 48
    _feed(db, live, cfg, [Observation("eurdep_gamma", stations[0], t, math.log(1.0))], t)
    assert open_alerts(db, src, live.candidates(t), t) == []  # one station alone: never
    _feed(db, live, cfg, [Observation("eurdep_gamma", stations[1], t, math.log(1.1))], t)
    (aid,) = open_alerts(db, src, live.candidates(t), t)
    ev = json.loads(db.execute("SELECT evidence FROM alerts WHERE alert_id = ?", (aid,)).fetchone()[0])
    assert {e["cell"] for e in ev} == set(stations)


async def test_poll_to_alert_in_one_call(db, sources, monkeypatch):
    """USGS significant (every_event): the poll that stores the quake opens
    the alert, with no timer in between."""
    from conftest import load_fixture

    cfg = sources["usgs_significant"]
    payload = load_fixture("usgs_sample.json")
    newest = max(f["properties"]["time"] for f in payload["features"]) // 1000
    live = LiveScorer(db, sources, now=newest)
    opened: list[int] = []

    async def on_new(c, obs, t):
        live.ingest(c, obs, t)
        opened.extend(open_alerts(db, sources, live.candidates(t), t))

    def handler(request):
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await poll_once(client, db, cfg, CacheValidators(), now=newest + 60, on_new=on_new)
    assert outcome.event == "ok" and outcome.rows_written > 0
    assert len(opened) == outcome.rows_written  # one alert per significant quake, same call


async def test_scoring_fault_is_recorded_not_raised(db, sources):
    from conftest import load_fixture

    cfg = sources["usgs_seismic"]

    async def on_new(c, obs, t):
        raise RuntimeError("model exploded")

    def handler(request):
        return httpx.Response(200, json=load_fixture("usgs_sample.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await poll_once(client, db, cfg, CacheValidators(), now=T0, on_new=on_new)
    assert outcome.event == "ok"
    ev = db.execute("SELECT detail FROM health WHERE event = 'score_error'").fetchone()
    assert "model exploded" in ev[0]


def test_registered_cells_score_zeros_before_any_report(db, sources):
    """The prober's countries are trained on quiet rounds from the start."""
    cfg = sources["probe_reachability"]
    w = native_seconds(cfg)
    cell = h3.latlng_to_cell(9.44, 7.50, 2)  # Nigeria
    live = LiveScorer(db, sources, now=T0)
    live.register_cells("probe_reachability", [cell], T0)
    for k in range(1, 6):
        live.tick(T0 + k * w + 61)
    rows = _native_rows(db, "probe_reachability")
    assert len(rows) == 5 and all(r["n_obs"] == 0 for r in rows)
