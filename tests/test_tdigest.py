"""t-digest: quantile accuracy on heavy-tailed data, merge, serialization."""

import numpy as np
import pytest

from worldwatch.cascade.tdigest import TDigest


def _rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def test_quantiles_uniform():
    xs = list(np.linspace(0, 1, 10000))
    d = TDigest.from_values(xs)
    for q in (0.1, 0.25, 0.5, 0.75, 0.9):
        assert d.quantile(q) == pytest.approx(q, abs=0.02)


def test_quantiles_heavy_tailed_rank_accuracy():
    # Pareto (heavy tail): t-digest guarantees RANK accuracy, not value accuracy
    # (a tiny rank error maps to a large value gap in a heavy tail). Rank error
    # is exactly what the PIT/q_value pipeline depends on.
    xs = (_rng(1).pareto(2.0, size=50000) + 1).tolist()
    d = TDigest.from_values(xs)
    arr = np.sort(np.array(xs))
    for q in (0.5, 0.9, 0.99, 0.999):
        v = d.quantile(q)
        true_rank = np.searchsorted(arr, v) / len(arr)
        assert abs(true_rank - q) <= 0.01, f"q={q}: rank {true_rank}"


def test_centroid_count_bounded():
    xs = list(_rng(2).normal(size=100000))
    d = TDigest.from_values(xs, delta=100.0)
    # merging digest stays compact regardless of input size
    assert d.n_centroids() <= 400


def test_merge_matches_pooled():
    a = _rng(3).normal(0, 1, 20000).tolist()
    b = _rng(4).normal(5, 2, 20000).tolist()
    da = TDigest.from_values(a)
    db = TDigest.from_values(b)
    da.merge(db)
    pooled = np.array(a + b)
    for q in (0.25, 0.5, 0.75, 0.95):
        assert da.quantile(q) == pytest.approx(np.quantile(pooled, q), abs=0.15)


def test_serialization_roundtrip():
    xs = _rng(5).normal(size=5000).tolist()
    d = TDigest.from_values(xs)
    blob = d.to_bytes()
    d2 = TDigest.from_bytes(blob)
    assert d2.n_centroids() == d.n_centroids()
    for q in (0.1, 0.5, 0.9):
        assert d2.quantile(q) == pytest.approx(d.quantile(q), abs=1e-9)


def test_empty_digest():
    d = TDigest()
    assert d.to_bytes() == b""
    assert TDigest.from_bytes(b"").n_centroids() == 0
    assert TDigest.from_bytes(None).n_centroids() == 0
    import math

    assert math.isnan(d.quantile(0.5))


def test_cdf_inverse_of_quantile():
    xs = list(np.linspace(0, 100, 10000))
    d = TDigest.from_values(xs)
    for x in (10.0, 50.0, 90.0):
        q = d.cdf(x)
        assert d.quantile(q) == pytest.approx(x, abs=2.0)
