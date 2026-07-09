"""Layer 0 — count observation flavor.

Online negative-binomial seasonal model for event counts (seismic, GDELT news,
Wikipedia pageviews, weather alerts). Fitted incrementally, constant time/memory
per bin (P4: no training runs).

    y_t ~ NegBinom(mean = μ_t · e_t,  size = r_t)

    μ_t : discounted mean of deseasonalized counts (EWMA)
    e_t : multiplicative seasonal exposure = f_hour[h(t)] · f_dow[d(t)],
          each profile learned online and renormalized to geometric mean 1
    r_t : dispersion "size", estimated online from the residual
          variance-to-mean ratio — r → ∞ recovers Poisson, finite r captures
          overdispersion. Honest for both regimes (P3).

Emits the PIT q_value via the RANDOMIZED PIT (Czado et al.): for discrete y,
q = F(y-1) + u·P(Y=y), u ~ U(0,1), which is exactly uniform under a correct
model — the ordinary CDF is not. u is drawn from a seeded generator so a run is
reproducible; historical q_values are frozen regardless (never re-scored).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np
from scipy.stats import nbinom

MODEL_VERSION = 1

_R_MAX = 1e6  # dispersion ceiling ≈ Poisson
_R_MIN = 1e-3
_EPS = 1e-9

HOUR_SECONDS = 3600
DAY_SECONDS = 86400


def hour_of_day(ts: int) -> int:
    return (ts // HOUR_SECONDS) % 24


def day_of_week(ts: int) -> int:
    return (ts // DAY_SECONDS + 4) % 7  # 1970-01-01 = Thursday; slot convention only


@dataclass
class NegBinomCount:
    """Online NB seasonal count model for one stream."""

    seasonal_hour: bool = False
    seasonal_dow: bool = False
    mean_lr: float = 0.05  # EWMA rate for the deseasonalized mean
    disp_lr: float = 0.02  # EWMA rate for the dispersion estimate
    seasonal_lr: float = 0.05
    seed: int = 12345

    # state
    _mu: float | None = None  # deseasonalized rate estimate
    _s2: float = 0.0  # EWMA of squared residual
    _mbar: float = 0.0  # EWMA of predictive mean
    _f_hour: np.ndarray = field(default_factory=lambda: np.ones(24), repr=False)
    _f_dow: np.ndarray = field(default_factory=lambda: np.ones(7), repr=False)
    _rng: np.random.Generator | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._rng is None:
            self._rng = np.random.default_rng(self.seed)

    @property
    def rate(self) -> float:
        """Current deseasonalized rate estimate (0.0 before first observation)."""
        return 0.0 if self._mu is None else self._mu

    def _exposure(self, ts: int) -> float:
        e = 1.0
        if self.seasonal_hour:
            e *= float(self._f_hour[hour_of_day(ts)])
        if self.seasonal_dow:
            e *= float(self._f_dow[day_of_week(ts)])
        return max(e, _EPS)

    def _dispersion_size(self) -> float:
        """Current NB size r from the variance-to-mean ratio."""
        if self._mbar <= _EPS:
            return _R_MAX
        d = self._s2 / self._mbar  # overdispersion index
        if d <= 1.0 + 1e-6:
            return _R_MAX  # under/equi-dispersed → Poisson limit
        return min(_R_MAX, max(_R_MIN, self._mbar / (d - 1.0)))

    def update(self, ts: int, count: float) -> float:
        y = float(count)
        e = self._exposure(ts)

        if self._mu is None:
            self._mu = max(y / e, 0.1)
            self._mbar = self._mu * e
            self._s2 = self._mbar  # Poisson prior
            return 0.5  # no predictive on first observation

        # --- one-step-ahead predictive (score before update) ---
        m = max(self._mu * e, _EPS)
        r = self._dispersion_size()
        p = r / (r + m)  # scipy nbinom: n=r, p → mean = r(1-p)/p = m
        q = self._randomized_pit(int(round(y)), r, p)

        # --- update deseasonalized mean, dispersion, seasonal profiles ---
        resid2 = (y - m) ** 2
        self._s2 += self.disp_lr * (resid2 - self._s2)
        self._mbar += self.disp_lr * (m - self._mbar)
        self._mu += self.mean_lr * (y / e - self._mu)
        self._mu = max(self._mu, _EPS)
        self._update_seasonal(ts, y, m)
        return q

    def _randomized_pit(self, y: int, r: float, p: float) -> float:
        assert self._rng is not None
        below = float(nbinom.cdf(y - 1, r, p)) if y > 0 else 0.0
        pmf = float(nbinom.pmf(y, r, p))
        u = float(self._rng.random())
        return min(1.0, max(0.0, below + u * pmf))

    def _update_seasonal(self, ts: int, y: float, m: float) -> None:
        ratio = (y + 0.5) / (m + 0.5)  # smoothed observed/expected
        log_r = math.log(ratio)
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
        gm = math.exp(float(np.mean(np.log(np.clip(f, _EPS, None)))))
        if gm > _EPS:
            f /= gm

    # --- serialization for the model_state BLOB ---

    def to_bytes(self) -> bytes:
        payload = {
            "v": MODEL_VERSION,
            "seasonal_hour": self.seasonal_hour,
            "seasonal_dow": self.seasonal_dow,
            "mean_lr": self.mean_lr,
            "disp_lr": self.disp_lr,
            "seasonal_lr": self.seasonal_lr,
            "seed": self.seed,
            "mu": self._mu,
            "s2": self._s2,
            "mbar": self._mbar,
            "f_hour": self._f_hour.tolist(),
            "f_dow": self._f_dow.tolist(),
        }
        return json.dumps(payload, separators=(",", ":")).encode()

    @classmethod
    def from_bytes(cls, blob: bytes) -> NegBinomCount:
        p = json.loads(blob)
        m = cls(
            seasonal_hour=p["seasonal_hour"],
            seasonal_dow=p["seasonal_dow"],
            mean_lr=p["mean_lr"],
            disp_lr=p["disp_lr"],
            seasonal_lr=p["seasonal_lr"],
            seed=p["seed"],
        )
        m._mu = p["mu"]
        m._s2 = p["s2"]
        m._mbar = p["mbar"]
        m._f_hour = np.array(p["f_hour"])
        m._f_dow = np.array(p["f_dow"])
        return m
