"""Merging t-digest (Dunning) for compact, mergeable quantile sketches.

Centroids are (mean, weight) pairs kept sorted by mean.  Compression uses the
k1 scale function so accuracy is highest in the tails — the right property for
a heavy-tailed anomaly system (P3: heavy tails are first-class).  Merges are
associative up to the compression bound, so consolidation order barely matters.

Serialized form (bins.sketch BLOB): little-endian float64 pairs
[mean0, weight0, mean1, weight1, ...]; empty digest → empty bytes.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Iterable

DEFAULT_DELTA = 100.0  # compression; larger = more centroids, more accuracy


class TDigest:
    __slots__ = ("delta", "_centroids")

    def __init__(self, delta: float = DEFAULT_DELTA) -> None:
        self.delta = delta
        # sorted by mean; list of [mean, weight]
        self._centroids: list[list[float]] = []

    # --- construction ---

    @classmethod
    def from_values(cls, values: Iterable[float], delta: float = DEFAULT_DELTA) -> TDigest:
        d = cls(delta)
        d.update(values)
        return d

    def update(self, values: Iterable[float]) -> None:
        incoming = sorted(float(v) for v in values)
        if not incoming:
            return
        merged = _merge_sorted(self._centroids, [[v, 1.0] for v in incoming])
        self._centroids = _compress(merged, self.delta)

    def merge(self, other: TDigest) -> None:
        if not other._centroids:
            return
        merged = _merge_sorted(self._centroids, [c[:] for c in other._centroids])
        self._centroids = _compress(merged, self.delta)

    # --- query ---

    @property
    def total_weight(self) -> float:
        return sum(c[1] for c in self._centroids)

    def n_centroids(self) -> int:
        return len(self._centroids)

    def quantile(self, q: float) -> float:
        """Value v such that ~fraction q of the mass lies at or below v."""
        cs = self._centroids
        if not cs:
            return math.nan
        if len(cs) == 1:
            return cs[0][0]
        w = self.total_weight
        target = q * w
        # cumulative weight at the *center* of each centroid
        cum = 0.0
        for i, (mean, weight) in enumerate(cs):
            center = cum + weight / 2.0
            if target <= center:
                if i == 0:
                    return cs[0][0]
                prev_mean, prev_weight = cs[i - 1]
                prev_center = cum - prev_weight / 2.0
                span = center - prev_center
                if span <= 0:
                    return mean
                frac = (target - prev_center) / span
                return prev_mean + frac * (mean - prev_mean)
            cum += weight
        return cs[-1][0]

    def cdf(self, x: float) -> float:
        """Fraction of mass at or below x (inverse of quantile)."""
        cs = self._centroids
        if not cs:
            return math.nan
        w = self.total_weight
        cum = 0.0
        for i, (mean, weight) in enumerate(cs):
            if x < mean:
                if i == 0:
                    return 0.0
                prev_mean = cs[i - 1][0]
                prev_center = cum - cs[i - 1][1] / 2.0
                center = cum + weight / 2.0
                if mean <= prev_mean:
                    return cum / w
                frac = (x - prev_mean) / (mean - prev_mean)
                return (prev_center + frac * (center - prev_center)) / w
            cum += weight
        return 1.0

    # --- serialization ---

    def to_bytes(self) -> bytes:
        flat: list[float] = []
        for mean, weight in self._centroids:
            flat.append(mean)
            flat.append(weight)
        return struct.pack(f"<{len(flat)}d", *flat)

    @classmethod
    def from_bytes(cls, blob: bytes | None, delta: float = DEFAULT_DELTA) -> TDigest:
        d = cls(delta)
        if not blob:
            return d
        vals = struct.unpack(f"<{len(blob) // 8}d", blob)
        d._centroids = [[vals[i], vals[i + 1]] for i in range(0, len(vals), 2)]
        return d


def _merge_sorted(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    """Merge two mean-sorted centroid lists into one mean-sorted list."""
    out: list[list[float]] = []
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i][0] <= b[j][0]:
            out.append(a[i][:])
            i += 1
        else:
            out.append(b[j][:])
            j += 1
    out.extend(c[:] for c in a[i:])
    out.extend(c[:] for c in b[j:])
    return out


def _k1(q: float, delta: float) -> float:
    """k1 scale function: maps quantile q∈[0,1] to scale space."""
    q = min(1.0, max(0.0, q))
    return delta / (2.0 * math.pi) * math.asin(2.0 * q - 1.0)


def _compress(centroids: list[list[float]], delta: float) -> list[list[float]]:
    """Merge adjacent centroids while they stay within one k1 scale unit."""
    if len(centroids) <= 1:
        return centroids
    total = sum(c[1] for c in centroids)
    if total <= 0:
        return centroids

    out: list[list[float]] = []
    cur_mean, cur_weight = centroids[0]
    q_left = 0.0
    for mean, weight in centroids[1:]:
        proposed = cur_weight + weight
        q_right = q_left + proposed / total
        if _k1(q_right, delta) - _k1(q_left, delta) <= 1.0:
            # absorb: weighted-mean update
            cur_mean = (cur_mean * cur_weight + mean * weight) / proposed
            cur_weight = proposed
        else:
            out.append([cur_mean, cur_weight])
            q_left += cur_weight / total
            cur_mean, cur_weight = mean, weight
    out.append([cur_mean, cur_weight])
    return out
