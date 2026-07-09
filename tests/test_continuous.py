"""Layer-0 continuous flavor: PIT calibration and outlier robustness.

Acceptance (p0-implementation-plan §Key algorithms 3):
- PIT uniform on synthetic Gaussian+seasonal data (KS p > 0.01).
- Miscalibration IS detected when generator and model disagree.
- Level not dragged by a single 10σ spike under heavy-tailed noise
  (vs a Gaussian baseline).
"""

import numpy as np
import pytest
from scipy.stats import kstest

from worldwatch.layer0.continuous import ContinuousSSM, make_harmonics

HOUR = 3600
DAY = 86400
T0 = 1_700_000_000


def _times(n: int, step: int = HOUR) -> list[int]:
    return [T0 + i * step for i in range(n)]


def _gaussian_seasonal_series(n, seed, sigma=1.0, level_walk=0.01, amp=3.0):
    """Level random walk + fixed daily seasonal + Gaussian noise.

    Matches the model's generative assumptions, so a correctly specified
    filter should produce uniform PIT values.
    """
    rng = np.random.default_rng(seed)
    ts = _times(n)
    level = 100.0
    ys = []
    for t in ts:
        level += rng.normal(0.0, np.sqrt(level_walk))
        phase = 2 * np.pi * (t - T0) / DAY
        seasonal = amp * np.sin(phase) + 0.5 * amp * np.cos(phase)
        ys.append(level + seasonal + rng.normal(0.0, sigma))
    return ts, ys


def _model(**overrides) -> ContinuousSSM:
    params = dict(
        harmonics=make_harmonics([DAY], n_harmonics=1),
        obs_scale=1.0,
        obs_dof=1e6,  # ≈ Gaussian
        level_var=0.01,
        trend_var=1e-7,
        seasonal_var=1e-7,
        time_scale=HOUR,
    )
    params.update(overrides)
    return ContinuousSSM(**params)


def test_pit_uniform_on_gaussian_seasonal():
    ts, ys = _gaussian_seasonal_series(2000, seed=7)
    m = _model()
    qs = [m.update(t, y) for t, y in zip(ts, ys, strict=False)]
    qs = np.array(qs[300:])  # discard burn-in
    p = kstest(qs, "uniform").pvalue
    assert p > 0.01, f"PIT not uniform: KS p={p:.4f}"
    assert 0.4 < qs.mean() < 0.6


def test_pit_uniform_on_student_t_noise():
    """Heavy-tailed t(5) noise, model with matching dof → still calibrated."""
    rng = np.random.default_rng(11)
    ts = _times(2500)
    level = 50.0
    ys = []
    for t in ts:
        level += rng.normal(0.0, 0.1)
        phase = 2 * np.pi * (t - T0) / DAY
        ys.append(level + 2.0 * np.sin(phase) + rng.standard_t(5))
    m = _model(obs_scale=1.0, obs_dof=5.0, level_var=0.01)
    qs = np.array([m.update(t, y) for t, y in zip(ts, ys, strict=False)][400:])
    p = kstest(qs, "uniform").pvalue
    assert p > 0.01, f"PIT not uniform under t(5): KS p={p:.4f}"


def test_miscalibration_is_detected():
    """Model σ far too small → innovations run to the tails → KS rejects."""
    ts, ys = _gaussian_seasonal_series(2000, seed=3, sigma=3.0)
    m = _model(obs_scale=0.5)  # claims σ=0.5 but truth is 3.0
    qs = np.array([m.update(t, y) for t, y in zip(ts, ys, strict=False)][300:])
    p = kstest(qs, "uniform").pvalue
    assert p < 0.01, f"miscalibration not detected: KS p={p:.4f}"


def test_level_not_dragged_by_spike():
    """A single 10σ spike must barely move the robust level, and far less
    than a Gaussian filter would allow."""
    n = 600
    ts = _times(n)
    rng = np.random.default_rng(5)
    ys = [100.0 + rng.standard_t(3) for _ in range(n)]  # heavy-tailed, level 100
    spike_i = 400
    ys[spike_i] = 100.0 + 10.0  # 10σ upward spike (σ≈1)

    robust = ContinuousSSM(obs_scale=1.0, obs_dof=3.0, level_var=0.01, time_scale=HOUR)
    gaussian = ContinuousSSM(obs_scale=1.0, obs_dof=1e6, level_var=0.01, time_scale=HOUR)

    for i, (t, y) in enumerate(zip(ts, ys, strict=False)):
        robust.update(t, y)
        gaussian.update(t, y)
        if i == spike_i:
            robust_jump = abs(robust.level - 100.0)
            gaussian_jump = abs(gaussian.level - 100.0)

    assert robust_jump < gaussian_jump, "robust filter should resist the spike more"
    assert robust_jump < 1.0, f"robust level dragged too far: {robust_jump:.3f}"


def test_serialization_roundtrip():
    ts, ys = _gaussian_seasonal_series(500, seed=9)
    m = _model()
    for t, y in zip(ts[:400], ys[:400], strict=False):
        m.update(t, y)

    restored = ContinuousSSM.from_bytes(m.to_bytes())
    # Predictions from both must match exactly going forward.
    for t in ts[400:410]:
        assert m.predict(t) == pytest.approx(restored.predict(t), abs=1e-9)


def test_first_observation_returns_half():
    m = _model()
    assert m.update(T0, 100.0) == 0.5
    assert m.level == 100.0
