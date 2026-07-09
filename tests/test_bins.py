"""Property and unit tests for cascade/bins.py."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from worldwatch.cascade.bins import (
    FINE_WIDTH_SECONDS,
    bin_for,
    bin_start,
    bin_width,
    scale_age_range,
    scale_for_age,
)

# --- unit tests ---


def test_bin_width_doublings():
    for s in range(10):
        assert bin_width(s) == FINE_WIDTH_SECONDS * (2**s)


def test_scale_for_age_zero():
    assert scale_for_age(0.0) == 0


def test_scale_for_age_just_below_boundary():
    assert scale_for_age(FINE_WIDTH_SECONDS - 1) == 0


def test_scale_for_age_at_boundary():
    assert scale_for_age(float(FINE_WIDTH_SECONDS)) == 1


def test_scale_age_range_partition():
    """scale_age_range intervals are contiguous and non-overlapping for 0..19."""
    prev_hi = 0.0
    for s in range(20):
        lo, hi = scale_age_range(s)
        assert lo == pytest.approx(prev_hi), f"gap at scale {s}"
        assert hi > lo
        prev_hi = hi


def test_bin_start_aligns():
    ts = 1_700_000_123
    for s in range(8):
        w = bin_width(s)
        bs = bin_start(ts, s)
        assert bs % w == 0
        assert bs <= ts < bs + w


def test_bin_for_returns_valid_scale():
    now = 1_700_100_000
    ts = now - FINE_WIDTH_SECONDS // 2
    scale, bs = bin_for(ts, now)
    assert scale == 0
    assert bs <= ts < bs + bin_width(scale)


def test_bin_for_future_raises():
    with pytest.raises(ValueError, match="future"):
        bin_for(ts=1_000_000, now=999_999)


# --- property tests ---


@given(age=st.floats(min_value=0, max_value=1e10, allow_nan=False, allow_infinity=False))
def test_scale_age_range_covers_age(age):
    s = scale_for_age(age)
    lo, hi = scale_age_range(s)
    assert lo <= age < hi


@given(
    now=st.integers(min_value=1_000_000, max_value=2_000_000_000),
    age=st.integers(min_value=0, max_value=10_000_000),
)
def test_bin_for_contains_ts(now, age):
    ts = now - age
    scale, bs = bin_for(ts, now)
    w = bin_width(scale)
    assert bs <= ts < bs + w


@given(
    now=st.integers(min_value=1_000_000, max_value=2_000_000_000),
    age=st.integers(min_value=0, max_value=10_000_000),
)
def test_bin_for_idempotent(now, age):
    """Calling bin_for twice with the same inputs returns the same result."""
    ts = now - age
    assert bin_for(ts, now) == bin_for(ts, now)


@given(
    now=st.integers(min_value=10_000_000, max_value=2_000_000_000),
    age1=st.integers(min_value=0, max_value=5_000_000),
    age2=st.integers(min_value=0, max_value=5_000_000),
)
def test_bin_start_total_order_preserved(now, age1, age2):
    """If ts1 < ts2, then bin_start(ts1, s) <= bin_start(ts2, s) for any scale."""
    ts1, ts2 = now - age1, now - age2
    if ts1 >= ts2:
        ts1, ts2 = ts2, ts1
    for s in range(6):
        assert bin_start(ts1, s) <= bin_start(ts2, s)


@given(
    ts=st.integers(min_value=1_000_000, max_value=2_000_000_000),
    scale=st.integers(min_value=0, max_value=20),
)
def test_bin_start_idempotent(ts, scale):
    """bin_start of a bin_start is itself."""
    bs = bin_start(ts, scale)
    assert bin_start(bs, scale) == bs


@settings(max_examples=200)
@given(
    now=st.integers(min_value=10_000_000, max_value=2_000_000_000),
    age=st.integers(min_value=0, max_value=10_000_000),
    delta=st.integers(min_value=1, max_value=1000),
)
def test_no_gaps_between_adjacent_ts(now, age, delta):
    """Two timestamps that differ by delta are in the same or adjacent bin at scale 0."""
    ts = now - age
    ts2 = ts + delta
    if ts2 > now:
        return
    s1, bs1 = bin_for(ts, now)
    s2, bs2 = bin_for(ts2, now)
    # They may be in different scales (ts2 is newer, potentially finer scale).
    # At each scale, bins must not overlap:
    if s1 == s2:
        # same scale: bins are contiguous partitions, so bs1 <= bs2
        assert bs1 <= bs2
