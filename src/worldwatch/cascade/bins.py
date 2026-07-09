"""Geometric cascade bin math.

A bin is identified by (scale, bin_start).  scale 0 is the finest resolution
(width = FINE_WIDTH_SECONDS); each higher scale doubles the width.  Bins are
age-dependent: an observation at time `ts` falls into the bin whose left edge
is the largest multiple of the bin width that is <= ts, for the scale whose
width covers the age bracket `now - ts`.

The cascade uses ~BINS_PER_OCTAVE bins per doubling of age.  Concretely:
  - scale 0 covers ages  [0,     W)         width W  = FINE_WIDTH_SECONDS
  - scale 1 covers ages  [W,    3W)         width 2W
  - scale 2 covers ages  [3W,   7W)         width 4W
  - scale k covers ages  [(2^k-1)W, (2^{k+1}-1)W)   width 2^k * W
so ~1 bin per doubling per scale, ~8 bins per octave when BINS_PER_OCTAVE=8.

Fine window (scale 0) is the raw ring.  Consolidation promotes observations
into progressively coarser bins as they age.
"""

from __future__ import annotations

import math

# Seconds — one 5-minute fine bin.
FINE_WIDTH_SECONDS: int = 300
# Number of fine bins before the first scale transition (~40 min of scale-0).
BINS_PER_OCTAVE: int = 8


def bin_width(scale: int) -> int:
    """Width in seconds of a bin at the given scale."""
    return FINE_WIDTH_SECONDS << scale


def scale_for_age(age_seconds: float) -> int:
    """Return the scale index for an observation of the given age.

    Scale k covers ages [(2^k - 1) * W, (2^{k+1} - 1) * W) where W is
    FINE_WIDTH_SECONDS.  Inverting: k = floor(log2(age/W + 1)).

    age_seconds = now - ts; must be >= 0.
    """
    if age_seconds < 0:
        raise ValueError(f"age_seconds must be >= 0, got {age_seconds}")
    if age_seconds < FINE_WIDTH_SECONDS:
        return 0
    return int(math.floor(math.log2(age_seconds / FINE_WIDTH_SECONDS + 1)))


def bin_start(ts: int, scale: int) -> int:
    """Left edge (epoch seconds) of the bin containing timestamp ts at scale."""
    w = bin_width(scale)
    return (ts // w) * w


def bin_for(ts: int, now: int) -> tuple[int, int]:
    """Return (scale, bin_start) for an observation at ts, evaluated at now."""
    age = now - ts
    if age < 0:
        raise ValueError(f"ts={ts} is in the future relative to now={now}")
    scale = scale_for_age(float(age))
    return scale, bin_start(ts, scale)


def scale_age_range(scale: int) -> tuple[float, float]:
    """Return [min_age, max_age) in seconds that map to this scale.

    max_age is float('inf') for the coarsest reachable scale.
    """
    lo = (2**scale - 1) * FINE_WIDTH_SECONDS
    hi = (2 ** (scale + 1) - 1) * FINE_WIDTH_SECONDS
    return float(lo), float(hi)


def all_scales_for_age_range(min_age: float, max_age: float) -> list[int]:
    """Scales that overlap with the age interval [min_age, max_age)."""
    scales = []
    s = 0
    while True:
        lo, hi = scale_age_range(s)
        if lo >= max_age:
            break
        if hi > min_age:
            scales.append(s)
        s += 1
        if s > 64:  # safety
            break
    return scales
