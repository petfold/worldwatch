"""Layer 0 — continuous observation flavor.

A dynamic harmonic regression state-space model fitted online by a robustified
Kalman recursion (constant time/memory per observation; no training runs, no
GPU — P4):

    y_t = level_t + Σ_j [c_j sin(ω_j τ) + d_j cos(ω_j τ)] + ε_t,   ε_t ~ t_ν(0, σ)

    level_t = level_{t-1} + Δt · trend_{t-1} + η        (local linear trend)
    trend_t = trend_{t-1} + ζ
    c_j, d_j : slowly-varying random-walk amplitudes

τ = ts − t0 (absolute time), so seasonal phase is exact under irregular
sampling and gaps — no assumption of a fixed step. Observation noise is
Student-t (P3: heavy tails first-class); a single variational step down-weights
outliers so the level is not dragged by spikes.

Emits the ONLY interchange currency (P3): the PIT q_value — the tail quantile
of y_t under the one-step-ahead predictive, in [0, 1], uniform iff calibrated.
The predictive is approximated as Student-t(location=ŷ, scale=√(ZPZᵀ+S), ν_t).

The observation variance is unknown and learned (West & Harrison's
unknown-variance recursion with discounting): S_t is its estimate with n_t
degrees of freedom of evidence, starting from the stanza's `obs_scale`² as a
prior guess worth `scale_prior_dof` observations and fading over
`scale_memory_seconds`. The predictive's degrees of freedom are
ν_t = min(n_t, obs_dof): while evidence on the scale is thin the predictive is
correspondingly wide; with ample evidence the configured heavy tail remains.

Process noise is relative (model v3): the stanza's level_var, trend_var and
seasonal_var are the absolute rates at its `obs_scale`, and scale with the
learned variance, Q_t = q · S_t / obs_scale². A stanza whose scale guess is
right behaves as before; one whose guess is off by 10x no longer gets a level
noise 100x its observation noise (which piled every PIT in the middle: 38 of
82 streams failed the nursery so, ADR 0007). W&H's model is scale-free the same
way: every variance is in units of the observation variance.

Values recorded to a `quantum` (whole centimetres, whole counts behind log1p)
get a randomized PIT, as counts do (ADR 0003): q uniform between the
predictive CDF at the quantum's edges, mapped through the stanza's
`transform`; `last_detect_q` is the least extreme value in that interval.
The noise variance is then learned net of the rounding's (quantum²/12,
Sheppard's correction), which the randomized PIT already accounts for.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np
from scipy.stats import t as student_t

MODEL_VERSION = 3


@dataclass
class Harmonic:
    period_seconds: float
    harmonic: int  # 1 = fundamental, 2 = first overtone, ...

    @property
    def omega(self) -> float:
        return 2.0 * math.pi * self.harmonic / self.period_seconds


def _least_extreme(q_lo: float, q_hi: float) -> float:
    """The least extreme PIT in [q_lo, q_hi] (as count.conservative_q)."""
    return q_lo if q_lo > 0.5 else q_hi if q_hi < 0.5 else 0.5


def make_harmonics(periods_seconds: list[float], n_harmonics: int = 1) -> list[Harmonic]:
    return [Harmonic(p, h) for p in periods_seconds for h in range(1, n_harmonics + 1)]


@dataclass
class ContinuousSSM:
    """Robust dynamic harmonic regression filter for one continuous stream."""

    harmonics: list[Harmonic] = field(default_factory=list)
    obs_scale: float = 1.0  # σ of the Student-t observation noise
    obs_dof: float = 4.0  # ν; smaller = heavier tails / more robust
    level_var: float = 1e-2  # process-noise rate per time unit
    trend_var: float = 1e-6
    seasonal_var: float = 1e-4
    time_scale: float = 3600.0  # seconds per internal time unit (level/trend)
    scale_prior_dof: float = 1.0  # how many observations the obs_scale guess is worth
    scale_memory_seconds: float = 7 * 86400.0  # e-folding time of scale evidence
    quantum: float = 0.0  # resolution of the raw values (0: continuous)
    transform: str = ""  # "", "log1p" or "log": raw → modelled value
    seed: int = 0  # the randomized PIT's stream (per cell)

    # state (initialized on first observation)
    _x: np.ndarray | None = field(default=None, repr=False)
    _P: np.ndarray | None = field(default=None, repr=False)
    _last_ts: int | None = None
    _t0: int | None = None
    _S: float | None = None  # observation-variance estimate
    _n: float | None = None  # its degrees of freedom (evidence)
    _rng: np.random.Generator | None = field(default=None, repr=False)
    last_detect_q: float | None = field(default=None, repr=False)  # of the last update

    @property
    def dim(self) -> int:
        return 2 + 2 * len(self.harmonics)

    @property
    def level(self) -> float:
        """Current level estimate (0.0 before the first observation)."""
        return 0.0 if self._x is None else float(self._x[0])

    # --- observation design vector Z_t ---

    def _z(self, ts: int) -> np.ndarray:
        z = np.zeros(self.dim)
        z[0] = 1.0  # level contributes to the observation
        # z[1] (trend) contributes 0 to the observation
        tau = float(ts - (self._t0 or ts))
        for i, h in enumerate(self.harmonics):
            z[2 + 2 * i] = math.sin(h.omega * tau)
            z[2 + 2 * i + 1] = math.cos(h.omega * tau)
        return z

    # --- one-step predict/update; returns the PIT q_value ---

    def update(self, ts: int, y: float) -> float:
        if self._x is None:
            self._init_state(ts, y)
            return 0.5  # no predictive on the first observation

        assert self._last_ts is not None and self._S is not None and self._n is not None
        dt = max(1e-9, (ts - self._last_ts) / self.time_scale)
        x_pred, P_pred = self._predict(dt)
        n = self._discounted_dof(ts)

        z = self._z(ts)
        yhat = float(z @ x_pred)
        state_var = float(z @ P_pred @ z)
        pred_var = state_var + self._S
        scale = math.sqrt(max(pred_var, 1e-12))

        df = min(n, self.obs_dof)
        rounding_var = 0.0
        if self.quantum > 0:
            lo_edge, hi_edge = self._edges(y)
            rounding_var = (hi_edge - lo_edge) ** 2 / 12  # Sheppard: rounding's own variance
            q_lo = float(student_t.cdf((lo_edge - yhat) / scale, df=df))
            q_hi = float(student_t.cdf((hi_edge - yhat) / scale, df=df))
            if self._rng is None:
                self._rng = np.random.default_rng(self.seed)
            q = q_lo + (q_hi - q_lo) * float(self._rng.random())
            self.last_detect_q = _least_extreme(q_lo, q_hi)
        else:
            q = float(student_t.cdf((y - yhat) / scale, df=df))
            self.last_detect_q = None

        w = self._robust_update(x_pred, P_pred, z, y, yhat, state_var)
        # learn the observation variance from the standardized innovation
        # (W&H: S ← S·(n + e²/Q)/(n + 1), with the robust weight as the count)
        # recorded values carry their rounding's variance on top of the noise's;
        # the randomized PIT already spreads over the quantum, so learn the noise
        # without it (Sheppard's correction), keeping the step positive
        e2 = max((y - yhat) ** 2 - rounding_var, -0.5 * pred_var)
        s_new = self._S * (n + w * e2 / pred_var) / (n + w)
        # state uncertainty is learned in units of the noise variance (W&H
        # scale C by S): when the scale estimate moves, so does P
        assert self._P is not None
        self._P = self._P * (s_new / self._S)
        self._S = s_new
        self._n = n + w
        self._last_ts = ts
        return q

    def predict(self, ts: int) -> tuple[float, float]:
        """One-step-ahead predictive (location, scale) without updating state."""
        if self._x is None or self._last_ts is None:
            return 0.0, math.inf
        dt = max(1e-9, (ts - self._last_ts) / self.time_scale)
        x_pred, P_pred = self._predict(dt)
        z = self._z(ts)
        yhat = float(z @ x_pred)
        s = self.obs_scale**2 if self._S is None else self._S
        scale = math.sqrt(max(float(z @ P_pred @ z) + s, 1e-12))
        return yhat, scale

    def _edges(self, y: float) -> tuple[float, float]:
        """The modelled values at the edges of y's quantum (raw ± quantum/2)."""
        half = self.quantum / 2
        if self.transform == "log1p":
            raw = math.expm1(y)
            return math.log1p(max(raw - half, -1 + 1e-12)), math.log1p(raw + half)
        if self.transform == "log":
            raw = math.exp(y)
            return math.log(max(raw - half, 1e-12)), math.log(raw + half)
        return y - half, y + half

    def _discounted_dof(self, ts: int) -> float:
        assert self._n is not None and self._last_ts is not None
        d = math.exp(-max(0, ts - self._last_ts) / self.scale_memory_seconds)
        return d * self._n + (1 - d) * self.scale_prior_dof

    # --- internals ---

    def _init_state(self, ts: int, y: float) -> None:
        self._t0 = ts
        self._last_ts = ts
        x = np.zeros(self.dim)
        x[0] = y
        self._x = x
        P = np.eye(self.dim) * 10.0
        P[1, 1] = 1.0  # trend starts near zero with modest uncertainty
        # prior state uncertainty in units of the noise variance (W&H), so the
        # first innovations are informative about the scale
        self._P = P * self.obs_scale**2
        self._S = self.obs_scale**2  # prior guess, worth scale_prior_dof observations
        self._n = self.scale_prior_dof

    def _transition(self, dt: float) -> np.ndarray:
        T = np.eye(self.dim)
        T[0, 1] = dt  # level += dt * trend
        return T  # seasonal amplitudes: identity (random walk)

    def _process_noise(self, dt: float) -> np.ndarray:
        q = np.zeros(self.dim)
        q[0] = self.level_var * dt
        q[1] = self.trend_var * dt
        q[2:] = self.seasonal_var * dt
        # relative: the configured rates hold at S = obs_scale², and scale with S
        rel = (self._S if self._S is not None else self.obs_scale**2) / self.obs_scale**2
        return np.diag(q * rel)

    def _predict(self, dt: float) -> tuple[np.ndarray, np.ndarray]:
        assert self._x is not None and self._P is not None
        T = self._transition(dt)
        x_pred = T @ self._x
        P_pred = T @ self._P @ T.T + self._process_noise(dt)
        return x_pred, P_pred

    def _robust_update(
        self,
        x_pred: np.ndarray,
        P_pred: np.ndarray,
        z: np.ndarray,
        y: float,
        yhat: float,
        state_var: float,
    ) -> float:
        """Kalman update with a Student-t variational weight; returns the weight."""
        assert self._S is not None
        gauss_var = state_var + self._S
        d2 = (y - yhat) ** 2 / max(gauss_var, 1e-12)
        w = (self.obs_dof + 1.0) / (self.obs_dof + d2)  # ∈ (0, 1]; ↓ for outliers
        r_eff = self._S / w  # inflate obs noise for down-weighted points

        f_eff = state_var + r_eff
        K = (P_pred @ z) / f_eff
        self._x = x_pred + K * (y - yhat)
        eye = np.eye(self.dim)
        self._P = (eye - np.outer(K, z)) @ P_pred
        return w

    # --- serialization for the model_state BLOB ---

    def to_bytes(self) -> bytes:
        payload = {
            "v": MODEL_VERSION,
            "harmonics": [(h.period_seconds, h.harmonic) for h in self.harmonics],
            "obs_scale": self.obs_scale,
            "obs_dof": self.obs_dof,
            "level_var": self.level_var,
            "trend_var": self.trend_var,
            "seasonal_var": self.seasonal_var,
            "time_scale": self.time_scale,
            "scale_prior_dof": self.scale_prior_dof,
            "scale_memory_seconds": self.scale_memory_seconds,
            "quantum": self.quantum,
            "transform": self.transform,
            "seed": self.seed,
            "rng": self._rng.bit_generator.state if self._rng is not None else None,
            "S": self._S,
            "n": self._n,
            "x": None if self._x is None else self._x.tolist(),
            "P": None if self._P is None else self._P.tolist(),
            "last_ts": self._last_ts,
            "t0": self._t0,
        }
        return json.dumps(payload, separators=(",", ":")).encode()

    @classmethod
    def from_bytes(cls, blob: bytes) -> ContinuousSSM:
        p = json.loads(blob)
        if p.get("v") != MODEL_VERSION:
            raise ValueError(f"continuous model state v{p.get('v')} != v{MODEL_VERSION}")
        m = cls(
            harmonics=[Harmonic(ps, h) for ps, h in p["harmonics"]],
            obs_scale=p["obs_scale"],
            obs_dof=p["obs_dof"],
            level_var=p["level_var"],
            trend_var=p["trend_var"],
            seasonal_var=p["seasonal_var"],
            time_scale=p["time_scale"],
            scale_prior_dof=p["scale_prior_dof"],
            scale_memory_seconds=p["scale_memory_seconds"],
            quantum=p["quantum"],
            transform=p["transform"],
            seed=p["seed"],
        )
        if p.get("rng") is not None:
            m._rng = np.random.default_rng(m.seed)
            m._rng.bit_generator.state = p["rng"]
        m._S = p["S"]
        m._n = p["n"]
        m._x = None if p["x"] is None else np.array(p["x"])
        m._P = None if p["P"] is None else np.array(p["P"])
        m._last_ts = p["last_ts"]
        m._t0 = p["t0"]
        return m
