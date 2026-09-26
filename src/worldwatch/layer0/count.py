"""Layer 0 — count observation flavor.

A Bayesian dynamic Gamma–Poisson model with unknown burstiness, fitted by
conjugate-style recursions (constant time/memory per bin; no training runs —
P4). The model carries its own uncertainty: a stream it has seen twice gets a
wide predictive, one it has seen a thousand times a narrow one, with no
warm-up special case anywhere.

    y_t | λ, ω_t ~ Poisson(λ · e_t · ω_t)
    ω_t | k      ~ Gamma(k, k)             per-bin burst factor, mean 1
    λ            ~ Gamma(a_t, b_t)         rate, discounted toward the prior
    k            ∈ K_GRID                  dispersion, posterior weights w_k

    e_t : multiplicative seasonal exposure = f_hour[h(t)] · f_dow[d(t)],
          each profile learned online and renormalized to geometric mean 1

Given k, y is negative binomial around λe; k = ∞ is Poisson (no bursts). The
one-step predictive integrates λ (Gamma posterior) and mixes over k by its
posterior weights, so its width reflects both how much the model has seen and
how bursty the stream has proven to be. News is bursty (one story → several
coded events, several outlets at once); seismic counts much less so — the
weights learn which.

Recursions per bin, for each k:
  discount   (a, b) ← δ·(a, b) + (1 − δ)·(a₀, b₀),  δ = exp(−Δt / memory)
             (old evidence fades; never more certain than ~memory/Δt bins)
  predict    p_k(y) = ∫ NB(y; k, λe) Gamma(λ; a, b) dλ   (closed form for k = ∞,
             quantile quadrature otherwise)
  weights    log w_k ← δ·log w_k + log p_k(y)   (forgetting, then evidence)
  update     E[ω | y] = (k + y) / (k + λ̂e);  a += y;  b += e·E[ω]
             (mean-field step: a burst inflates exposure, not the rate)

Emits the PIT q_value via the RANDOMIZED PIT (Czado et al.): for discrete y,
q = F(y-1) + u·P(Y=y), u ~ U(0,1), which is exactly uniform under a correct
model. The first bin has no informative predictive (the prior is vague by
design) and returns 0.5.
"""

from __future__ import annotations

import json
import math
import zlib
from dataclasses import dataclass, field

import numpy as np
from scipy.special import logsumexp
from scipy.stats import gamma as gamma_dist
from scipy.stats import nbinom

MODEL_VERSION = 2
LEGACY_SEED = 12345  # once shared by every model: all cells drew the same u


def cell_seed(stream_id: str, cell: str) -> int:
    """A stable seed per (stream, cell). The randomized PIT's u must be
    independent across cells — with one shared sequence, every cell reporting
    zero got the same q in the same window, a fake coherent anomaly."""
    return zlib.crc32(f"{stream_id}|{cell}".encode())

# Dispersion hypotheses: NB size k (smaller = burstier); inf = Poisson.
K_GRID: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, math.inf)
_N_NODES = 24  # quantile quadrature nodes over λ for finite k
_EPS = 1e-12

HOUR_SECONDS = 3600
DAY_SECONDS = 86400


def hour_of_day(ts: int) -> int:
    return (ts // HOUR_SECONDS) % 24


def day_of_week(ts: int) -> int:
    return (ts // DAY_SECONDS + 4) % 7  # 1970-01-01 = Thursday; slot convention only


@dataclass
class BayesianCount:
    """Dynamic Gamma–Poisson count model with a posterior over burstiness."""

    seasonal_hour: bool = False
    seasonal_dow: bool = False
    memory_seconds: float = 3 * DAY_SECONDS  # e-folding time of the rate's evidence
    prior_shape: float = 0.5  # a₀ — Jeffreys-like, vague
    prior_rate: float = 1e-3  # b₀ — per unit exposure; vague
    seasonal_lr: float = 0.05
    seed: int = LEGACY_SEED
    k_grid: tuple[float, ...] = K_GRID  # dispersion hypotheses (tests pin this)

    # state
    _a: np.ndarray | None = field(default=None, repr=False)
    _b: np.ndarray | None = field(default=None, repr=False)
    _logw: np.ndarray | None = field(default=None, repr=False)
    _last_ts: int | None = None
    _f_hour: np.ndarray = field(default_factory=lambda: np.ones(24), repr=False)
    _f_dow: np.ndarray = field(default_factory=lambda: np.ones(7), repr=False)
    _rng: np.random.Generator | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._rng is None:
            self._rng = np.random.default_rng(self.seed)
        if self._logw is None:
            self._logw = np.full(len(self.k_grid), -math.log(len(self.k_grid)))

    # --- summaries ---

    @property
    def weights(self) -> np.ndarray:
        return np.exp(self._logw - logsumexp(self._logw))

    @property
    def rate(self) -> float:
        """Posterior-mean deseasonalized rate (0.0 before the first observation)."""
        if self._a is None or self._b is None:
            return 0.0
        return float(np.sum(self.weights * self._a / self._b))

    def dispersion_size(self) -> float:
        """Posterior-mode NB size k (inf = Poisson)."""
        return self.k_grid[int(np.argmax(self._logw))]

    # --- the recursion ---

    def update(self, ts: int, count: float) -> float:
        y = int(round(float(count)))
        e = self._exposure(ts)

        if self._a is None or self._b is None or self._last_ts is None:
            n = len(self.k_grid)
            self._a = np.full(n, self.prior_shape + y, dtype=float)
            self._b = np.full(n, self.prior_rate + e, dtype=float)
            self._last_ts = ts
            return 0.5  # vague prior: no informative predictive for the first bin

        delta = math.exp(-max(0, ts - self._last_ts) / self.memory_seconds)
        a = delta * self._a + (1 - delta) * self.prior_shape
        b = delta * self._b + (1 - delta) * self.prior_rate

        pmf, below = self._predictive(y, e, a, b)
        w = self.weights
        p_y = float(np.sum(w * pmf))
        f_below = float(np.sum(w * below))
        assert self._rng is not None
        q = min(1.0, max(0.0, f_below + float(self._rng.random()) * p_y))

        # weights: forget a little, then weigh the evidence
        self._logw = delta * self._logw + np.log(np.clip(pmf, _EPS, None))
        self._logw -= logsumexp(self._logw)

        # rate posterior per hypothesis (mean-field over the burst factor)
        lam_hat = a / b
        k = np.array(self.k_grid)
        with np.errstate(invalid="ignore"):  # k = inf rows take the Poisson branch
            e_omega = np.where(np.isinf(k), 1.0, (k + y) / (k + lam_hat * e))
        self._a = a + y
        self._b = b + e * e_omega
        self._last_ts = ts

        self._update_seasonal(ts, y, float(np.sum(w * lam_hat)) * e)
        return q

    def _predictive(
        self, y: int, e: float, a: np.ndarray, b: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """Per-hypothesis P(Y = y) and P(Y < y) under the λ-integrated predictive
        (vectorized over the dispersion grid: four scipy calls per bin)."""
        k = np.array(self.k_grid)
        pois = np.isinf(k)
        pmf = np.empty(len(k))
        below = np.zeros(len(k))
        if pois.any():  # Gamma–Poisson: exactly NB(size a, p = b / (b + e))
            p = b[pois] / (b[pois] + e)
            pmf[pois] = nbinom.pmf(y, a[pois], p)
            if y > 0:
                below[pois] = nbinom.cdf(y - 1, a[pois], p)
        fin = ~pois
        if fin.any():  # quantile quadrature over λ ~ Gamma(a, b)
            u = (np.arange(_N_NODES) + 0.5) / _N_NODES
            lam = gamma_dist.ppf(u[None, :], a[fin, None], scale=1.0 / b[fin, None])
            kk = k[fin, None]
            p = kk / (kk + np.maximum(lam * e, _EPS))
            pmf[fin] = nbinom.pmf(y, kk, p).mean(axis=1)
            if y > 0:
                below[fin] = nbinom.cdf(y - 1, kk, p).mean(axis=1)
        return pmf, below

    # --- seasonality (exposure) ---

    def _exposure(self, ts: int) -> float:
        e = 1.0
        if self.seasonal_hour:
            e *= float(self._f_hour[hour_of_day(ts)])
        if self.seasonal_dow:
            e *= float(self._f_dow[day_of_week(ts)])
        return max(e, 1e-9)

    def _update_seasonal(self, ts: int, y: float, m: float) -> None:
        if not (self.seasonal_hour or self.seasonal_dow):
            return
        log_r = math.log((y + 0.5) / (m + 0.5))  # smoothed observed/expected
        if self.seasonal_hour:
            h = hour_of_day(ts)
            self._f_hour[h] *= math.exp(self.seasonal_lr * log_r)
            self._renormalize(self._f_hour)
        if self.seasonal_dow:
            d = day_of_week(ts)
            self._f_dow[d] *= math.exp(self.seasonal_lr * log_r)
            self._renormalize(self._f_dow)

    @staticmethod
    def _renormalize(f: np.ndarray) -> None:
        """Rescale a profile to geometric mean 1 (in place)."""
        gm = math.exp(float(np.mean(np.log(np.clip(f, 1e-9, None)))))
        if gm > 1e-9:
            f /= gm

    # --- serialization for the model_state BLOB ---

    def to_bytes(self) -> bytes:
        payload = {
            "v": MODEL_VERSION,
            "seasonal_hour": self.seasonal_hour,
            "seasonal_dow": self.seasonal_dow,
            "memory_seconds": self.memory_seconds,
            "prior_shape": self.prior_shape,
            "prior_rate": self.prior_rate,
            "seasonal_lr": self.seasonal_lr,
            "seed": self.seed,
            "k_grid": [None if math.isinf(k) else k for k in self.k_grid],
            "a": None if self._a is None else self._a.tolist(),
            "b": None if self._b is None else self._b.tolist(),
            "logw": self._logw.tolist(),
            "last_ts": self._last_ts,
            "f_hour": self._f_hour.tolist(),
            "f_dow": self._f_dow.tolist(),
            "rng": self._rng.bit_generator.state if self._rng is not None else None,
        }
        return json.dumps(payload, separators=(",", ":")).encode()

    @classmethod
    def from_bytes(cls, blob: bytes) -> BayesianCount:
        p = json.loads(blob)
        if p.get("v") != MODEL_VERSION:
            raise ValueError(f"count model state v{p.get('v')} != v{MODEL_VERSION}")
        m = cls(
            k_grid=tuple(math.inf if k is None else float(k) for k in p["k_grid"]),
            seasonal_hour=p["seasonal_hour"],
            seasonal_dow=p["seasonal_dow"],
            memory_seconds=p["memory_seconds"],
            prior_shape=p["prior_shape"],
            prior_rate=p["prior_rate"],
            seasonal_lr=p["seasonal_lr"],
            seed=p["seed"],
        )
        m._a = None if p["a"] is None else np.array(p["a"])
        m._b = None if p["b"] is None else np.array(p["b"])
        m._logw = np.array(p["logw"])
        m._last_ts = p["last_ts"]
        m._f_hour = np.array(p["f_hour"])
        m._f_dow = np.array(p["f_dow"])
        if p.get("rng") is not None:
            assert m._rng is not None
            m._rng.bit_generator.state = p["rng"]
        return m

    def reseed(self, seed: int) -> None:
        self.seed = seed
        self._rng = np.random.default_rng(seed)
