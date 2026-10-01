"""Pooling a count stream's cells over the H3 tree (ADR 0005)."""

import dataclasses
import math

import h3
import numpy as np
import pytest
from scipy.stats import kstest

from worldwatch.ingest.models import Observation
from worldwatch.layer0.count import BayesianCount
from worldwatch.layer0.live import LiveScorer
from worldwatch.layer0.native import NATIVE_SCALE, native_seconds
from worldwatch.layer0.pool import POOL_SCALE, POOLED_VERSION, PoolSettings, TreePool, predictive
from worldwatch.store import write_new_observations

T0 = 1_999_999_800
W = 300
PARENT = h3.latlng_to_cell(35.7, 139.7, 1)  # 49 resolution-3 cells under it
CELLS = sorted(h3.cell_to_children(PARENT, 3))


def _pool(rho, cells=CELLS):
    models = {c: BayesianCount(seed=i + 1) for i, c in enumerate(cells)}
    pool = TreePool(PoolSettings(rho=rho), cell_model=models.__getitem__, new_model=lambda node: BayesianCount(seed=0))
    return pool, models


def test_settings_from_model_table():
    assert PoolSettings.from_model_table({}) is None
    s = PoolSettings.from_model_table({"pool": "h3", "pool_rho": 0.2, "pool_memory_seconds": 3600})
    assert (s.rho, s.memory_seconds, s.coarsest) == (0.2, 3600.0, 0)
    with pytest.raises(ValueError):
        PoolSettings.from_model_table({"pool": "voronoi"})


def test_predictive_matches_the_count_model():
    rng = np.random.default_rng(0)
    m = BayesianCount()
    for _ in range(200):
        a = 0.5 + rng.gamma(0.7, 40.0, 10)
        b = 1e-3 + rng.gamma(0.7, 400.0, 10)
        y, e = int(rng.choice([0, 0, 1, 3, 12])), float(rng.choice([1.0, 0.3, 0.02]))
        pm_ref, bl_ref = m._predictive(y, e, a, b)
        pm, bl = predictive(np.array([y]), np.array([e]), a[None, :], b[None, :])
        assert np.allclose(pm[0], pm_ref, rtol=1e-5, atol=1e-300)
        assert np.allclose(bl[0], bl_ref, atol=1e-7)


def test_never_pooling_is_layer0_alone():
    """With rho near 0 every cell is its own bin: the pooled q's are the cell models' own."""
    cells = CELLS[:7]
    pool, models = _pool(1e-12, cells)
    ref = {c: BayesianCount(seed=i + 1) for i, c in enumerate(cells)}
    rng = np.random.default_rng(1)
    rates = rng.gamma(1.0, 0.05, len(cells))
    for t in range(400):
        ys = {c: int(rng.poisson(r)) for c, r in zip(cells, rates, strict=True)}
        out = pool.step(T0 + W * t, ys, cells)
        for c in cells:
            q_ref = ref[c].update(T0 + W * t, ys[c])
            assert out[c][0] == pytest.approx(q_ref, abs=1e-6)
            assert out[c][1] == pytest.approx(ref[c].last_detect_q, abs=1e-6)
    for c in cells:
        assert np.allclose(models[c]._a, ref[c]._a) and np.allclose(models[c]._b, ref[c]._b)
        assert np.allclose(models[c]._logw, ref[c]._logw, atol=1e-5)


def test_weights_follow_the_data():
    """A hot cell keeps its own model; quiet cells lean on their ancestors."""
    pool, _ = _pool(0.1)
    rng = np.random.default_rng(2)
    hot = CELLS[0]
    for t in range(1500):
        ys = {c: int(rng.poisson(2.0 if c == hot else 0.002)) for c in CELLS}
        pool.step(T0 + W * t, ys, CELLS)
    w = pool.last_weights
    for c in CELLS:
        assert sum(w[c].values()) == pytest.approx(1.0)
    assert w[hot][hot] > 0.99
    quiet = [c for c in CELLS if c != hot]
    assert np.mean([1 - w[c][c] for c in quiet]) > 0.5


def test_pooling_calibrates_sparse_cells():
    """49 sparse cells with one rate: alone, each cell's model stays wide and its upper tail
    too thin (the replay's finding); pooled, the tail comes to the nominal mass."""
    rng = np.random.default_rng(3)
    T = 2600
    ys = rng.poisson(2e-4, (T, len(CELLS)))
    tails, qs = {}, {}
    for rho in (1e-12, 0.1):
        pool, _ = _pool(rho)
        q = []
        for t in range(T):
            out = pool.step(T0 + W * t, dict(zip(CELLS, ys[t].tolist(), strict=True)), CELLS)
            if t >= 576:
                q.extend(out[c][0] for c in CELLS)
        qs[rho] = np.array(q)
        tails[rho] = float(np.mean(qs[rho] > 0.999))
    assert abs(tails[0.1] - 0.001) < abs(tails[1e-12] - 0.001)
    assert 0.0005 < tails[0.1] < 0.002
    assert kstest(qs[0.1], "uniform").statistic < 0.01


def test_live_pooled_stream(db, sources, tmp_path):
    """A stanza with pool = "h3": pooled q's in the archive, the pool's states saved, and a
    restarted scorer carrying on exactly where an uninterrupted one would be."""
    from worldwatch.db import open_db

    base = sources["usgs_seismic"]
    model = dict(base.extra.get("model", {}), pool="h3")
    cfg = dataclasses.replace(base, extra=dict(base.extra, model=model))
    srcs = dict(sources, usgs_seismic=cfg)
    w = native_seconds(cfg)
    a, b = CELLS[0], CELLS[1]

    def feed(conn, live, t, cell, n):
        obs = [Observation("usgs_seismic", cell, t + i, 2.0) for i in range(n)]
        live.ingest(cfg, write_new_observations(conn, obs, now=t), t)

    def run(conn, live, steps):
        for k in steps:
            t = T0 + k * w
            if k % 5 == 0:
                feed(conn, live, t + 10, a, 2)
            if k % 17 == 3:
                feed(conn, live, t + 20, b, 1)
            live.tick(t + w + 61)

    other = open_db(tmp_path / "other.db")
    run(other, LiveScorer(other, srcs, now=T0, grace_seconds=60), range(60))
    live = LiveScorer(db, srcs, now=T0, grace_seconds=60)
    run(db, live, range(30))
    run(db, LiveScorer(db, srcs, now=T0 + 30 * w, grace_seconds=60), range(30, 60))  # a restart

    q = "SELECT cell, bin_start, q_value, q_detect, model_version FROM surprise WHERE scale = ? ORDER BY cell, bin_start"
    got, want = db.execute(q, (NATIVE_SCALE,)).fetchall(), other.execute(q, (NATIVE_SCALE,)).fetchall()
    assert [tuple(r) for r in got] == [tuple(r) for r in want]
    assert {r["model_version"] for r in got} == {POOLED_VERSION}
    pool_nodes = {r["cell"] for r in db.execute("SELECT cell FROM model_state WHERE scale = ?", (POOL_SCALE,))}
    assert {h3.cell_to_parent(a, 2), h3.cell_to_parent(a, 0), "empty"} <= pool_nodes
    assert math.isfinite(got[-1]["q_value"])
