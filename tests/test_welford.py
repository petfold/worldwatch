"""Welford moments: correctness vs numpy, and merge commutativity/associativity."""

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from worldwatch.cascade import welford

floats = st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False)


def test_from_values_matches_numpy():
    xs = [1.0, 2.0, 3.0, 4.0, 100.0]
    m = welford.from_values(xs)
    assert m.n == 5
    assert m.mean == pytest.approx(np.mean(xs))
    assert m.variance == pytest.approx(np.var(xs))  # population variance
    assert m.vmin == 1.0
    assert m.vmax == 100.0


def test_empty():
    m = welford.from_values([])
    assert m.is_empty()
    assert m.variance == 0.0


@given(st.lists(floats, min_size=1, max_size=200))
def test_variance_matches_numpy(xs):
    m = welford.from_values(xs)
    assert m.mean == pytest.approx(np.mean(xs), rel=1e-6, abs=1e-6)
    assert m.variance == pytest.approx(np.var(xs), rel=1e-6, abs=1e-6)


@given(st.lists(floats, min_size=0, max_size=100), st.lists(floats, min_size=0, max_size=100))
def test_merge_equals_combined(a, b):
    ma, mb = welford.from_values(a), welford.from_values(b)
    merged = welford.merge(ma, mb)
    combined = welford.from_values(a + b)
    assert merged.n == combined.n
    assert merged.mean == pytest.approx(combined.mean, rel=1e-6, abs=1e-6)
    assert merged.m2 == pytest.approx(combined.m2, rel=1e-5, abs=1e-5)


@given(st.lists(floats, min_size=1, max_size=80), st.lists(floats, min_size=1, max_size=80))
def test_merge_commutative(a, b):
    ma, mb = welford.from_values(a), welford.from_values(b)
    left = welford.merge(ma, mb)
    right = welford.merge(mb, ma)
    assert left.n == right.n
    assert left.mean == pytest.approx(right.mean, rel=1e-9, abs=1e-9)
    assert left.m2 == pytest.approx(right.m2, rel=1e-6, abs=1e-6)
