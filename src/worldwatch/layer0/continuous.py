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
The predictive is approximated as Student-t(location=ŷ, scale=√(ZPZᵀ+σ²), ν).

Observation scale σ is provided (set by onboarding type/seasonality inference,
architecture §4); this model treats it as fixed. Online scale/nursery
machinery is P1.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np
from scipy.stats import t as student_t

MODEL_VERSION = 1


@dataclass
class Harmonic:
    period_seconds: float
    harmonic: int  # 1 = fundamental, 2 = first overtone, ...

    @property
    def omega(self) -> float:
        return 2.0 * math.pi * self.harmonic / self.period_seconds


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

    # state (initialized on first observation)
    _x: np.ndarray | None = field(default=None, repr=False)
    _P: np.ndarray | None = field(default=None, repr=False)
    _last_ts: int | None = None
    _t0: int | None = None

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

        assert self._last_ts is not None
        dt = max(1e-9, (ts - self._last_ts) / self.time_scale)
        x_pred, P_pred = self._predict(dt)

        z = self._z(ts)
        yhat = float(z @ x_pred)
        state_var = float(z @ P_pred @ z)
        pred_var = state_var + self.obs_scale**2
        scale = math.sqrt(max(pred_var, 1e-12))

        std_innov = (y - yhat) / scale
        q = float(student_t.cdf(std_innov, df=self.obs_dof))

        self._robust_update(x_pred, P_pred, z, y, yhat, state_var)
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
        scale = math.sqrt(max(float(z @ P_pred @ z) + self.obs_scale**2, 1e-12))
        return yhat, scale

    # --- internals ---

    def _init_state(self, ts: int, y: float) -> None:
        self._t0 = ts
        self._last_ts = ts
        x = np.zeros(self.dim)
        x[0] = y
        self._x = x
        P = np.eye(self.dim) * 10.0
        P[1, 1] = 1.0  # trend starts near zero with modest uncertainty
        self._P = P

    def _transition(self, dt: float) -> np.ndarray:
        T = np.eye(self.dim)
        T[0, 1] = dt  # level += dt * trend
        return T  # seasonal amplitudes: identity (random walk)

    def _process_noise(self, dt: float) -> np.ndarray:
        q = np.zeros(self.dim)
        q[0] = self.level_var * dt
        q[1] = self.trend_var * dt
        q[2:] = self.seasonal_var * dt
        return np.diag(q)

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
    ) -> None:
        # One variational step: weight from the Student-t scale mixture.
        gauss_var = state_var + self.obs_scale**2
        d2 = (y - yhat) ** 2 / max(gauss_var, 1e-12)
        w = (self.obs_dof + 1.0) / (self.obs_dof + d2)  # ∈ (0, 1]; ↓ for outliers
        r_eff = self.obs_scale**2 / w  # inflate obs noise for down-weighted points

        f_eff = state_var + r_eff
        K = (P_pred @ z) / f_eff
        self._x = x_pred + K * (y - yhat)
        eye = np.eye(self.dim)
        self._P = (eye - np.outer(K, z)) @ P_pred

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
            "x": None if self._x is None else self._x.tolist(),
            "P": None if self._P is None else self._P.tolist(),
            "last_ts": self._last_ts,
            "t0": self._t0,
        }
        return json.dumps(payload, separators=(",", ":")).encode()

    @classmethod
    def from_bytes(cls, blob: bytes) -> ContinuousSSM:
        p = json.loads(blob)
        m = cls(
            harmonics=[Harmonic(ps, h) for ps, h in p["harmonics"]],
            obs_scale=p["obs_scale"],
            obs_dof=p["obs_dof"],
            level_var=p["level_var"],
            trend_var=p["trend_var"],
            seasonal_var=p["seasonal_var"],
            time_scale=p["time_scale"],
        )
        m._x = None if p["x"] is None else np.array(p["x"])
        m._P = None if p["P"] is None else np.array(p["P"])
        m._last_ts = p["last_ts"]
        m._t0 = p["t0"]
        return m
