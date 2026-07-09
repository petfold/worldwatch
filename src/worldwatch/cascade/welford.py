"""Welford moment accumulator with a commutative/associative parallel merge.

Holds count/mean/M2 (→ variance) plus min/max per bin.  The merge is Chan's
parallel algorithm, so folding batches in any order — or re-folding across
consolidation runs — yields identical results (consolidator commutativity).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(slots=True)
class Moments:
    n: int = 0
    mean: float = 0.0
    m2: float = 0.0
    vmin: float = math.inf
    vmax: float = -math.inf

    @property
    def variance(self) -> float:
        """Population variance (0 when n < 2)."""
        return self.m2 / self.n if self.n > 1 else 0.0

    def is_empty(self) -> bool:
        return self.n == 0


def from_values(values: Iterable[float]) -> Moments:
    m = Moments()
    for x in values:
        n = m.n + 1
        delta = x - m.mean
        mean = m.mean + delta / n
        m.m2 += delta * (x - mean)
        m.mean = mean
        m.n = n
        if x < m.vmin:
            m.vmin = x
        if x > m.vmax:
            m.vmax = x
    return m


def merge(a: Moments, b: Moments) -> Moments:
    """Combine two moment accumulators (Chan et al. parallel variance)."""
    if a.n == 0:
        return Moments(b.n, b.mean, b.m2, b.vmin, b.vmax)
    if b.n == 0:
        return Moments(a.n, a.mean, a.m2, a.vmin, a.vmax)
    n = a.n + b.n
    delta = b.mean - a.mean
    mean = a.mean + delta * b.n / n
    m2 = a.m2 + b.m2 + delta * delta * a.n * b.n / n
    return Moments(n, mean, m2, min(a.vmin, b.vmin), max(a.vmax, b.vmax))
