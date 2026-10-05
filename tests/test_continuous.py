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


def test_wrong_scale_guess_is_learned():
    """obs_scale is only a prior guess: σ claimed 0.5, truth 3.0 → the model
    learns the scale and ends up calibrated (it used to stay wrong forever)."""
    ts, ys = _gaussian_seasonal_series(2000, seed=3, sigma=3.0)
    m = _model(obs_scale=0.5)
    qs = np.array([m.update(t, y) for t, y in zip(ts, ys, strict=False)][300:])
    assert kstest(qs, "uniform").pvalue > 0.01
    assert 2.5 < m._S**0.5 < 3.5


def test_miscalibration_is_detected():
    """A mismatch learning can't fix — Cauchy noise under a Gaussian noise
    model — must still show up as non-uniform PIT."""
    ts = _times(2000)
    rng = np.random.default_rng(3)
    ys = 100.0 + rng.standard_cauchy(len(ts))
    m = _model(harmonics=[])  # Gaussian noise (obs_dof ≈ ∞)
    qs = np.array([m.update(t, y) for t, y in zip(ts, ys, strict=False)][300:])
    p = kstest(qs, "uniform").pvalue
    assert p < 0.01, f"miscalibration not detected: KS p={p:.4f}"


def test_cold_start_not_overconfident_with_a_fair_guess():
    """Fresh filters with a scale guess of the right order: the first bins
    stay within calibration — uncertainty is carried, not special-cased."""
    early = []
    for seed in range(150):
        rng = np.random.default_rng(seed)
        ys = 5 + np.cumsum(rng.normal(0, 0.05, 25)) + rng.normal(0, 1.0, 25)
        m = ContinuousSSM(obs_scale=0.7, level_var=0.0025, time_scale=HOUR)
        early.extend(m.update(T0 + i * HOUR, y) for i, y in enumerate(ys))
    e = np.array(early[1:])
    assert np.mean((e < 0.01) | (e > 0.99)) <= 0.03


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


def _tv_tails(qs):
    h = np.histogram(qs, bins=10, range=(0, 1))[0] / len(qs)
    return 0.5 * np.abs(h - 0.1).sum(), (qs <= 0.01).mean() / 0.01, (qs >= 0.99).mean() / 0.01


def test_process_noise_follows_the_learned_scale():
    """Model v3: a stanza whose scale guess is 10x too big (σ guessed 1, truth
    0.1, with the level walk in proportion) is still calibrated. With process
    noise in absolute units (v2) the level noise was 100x the true observation
    noise and every PIT sat near 0.5 (Cloudflare Radar, BTC, rivers: ADR 0007)."""
    ts, ys = _gaussian_seasonal_series(3000, seed=21, sigma=0.1, level_walk=1e-4, amp=0.3)
    m = _model(obs_scale=1.0, level_var=0.01)
    qs = np.array([m.update(t, y) for t, y in zip(ts, ys, strict=False)][500:])
    tv, lo, hi = _tv_tails(qs)
    assert tv < 0.05 and 0.5 < lo < 2 and 0.5 < hi < 2, (tv, lo, hi)


def _rounded_series(n, seed):
    """Whole-unit readings (a slow gauge in cm): a level walk plus noise of 0.3 units, rounded."""
    rng = np.random.default_rng(seed)
    level, ys = 120.0, []
    for _ in range(n):
        level += rng.normal(0.0, 0.05)
        ys.append(float(round(level + rng.normal(0.0, 0.3))))
    return _times(n), ys


def test_quantized_values_get_a_randomized_pit():
    ts, ys = _rounded_series(4000, seed=22)
    plain = _model(harmonics=[], obs_scale=0.4, level_var=0.0025)
    qs = np.array([plain.update(t, y) for t, y in zip(ts, ys, strict=False)][500:])
    assert _tv_tails(qs)[0] > 0.05  # whole units pile PITs up near repeated values
    m = _model(harmonics=[], obs_scale=0.4, level_var=0.0025, quantum=1.0, seed=5)
    qs = np.array([m.update(t, y) for t, y in zip(ts, ys, strict=False)][500:])
    tv, lo, hi = _tv_tails(qs)
    assert tv < 0.05 and 0.5 < lo < 2 and 0.5 < hi < 2, (tv, lo, hi)
    assert m.last_detect_q is not None  # detection reads the least extreme value


def test_quantum_edges_follow_the_transform():
    m = _model(quantum=1.0, transform="log1p")
    lo, hi = m._edges(float(np.log1p(4.0)))
    assert np.isclose(lo, np.log1p(3.5)) and np.isclose(hi, np.log1p(4.5))
    lo, hi = m._edges(0.0)  # a zero count: the lower edge stays above log1p(-1)
    assert np.isfinite(lo) and np.isclose(hi, np.log1p(0.5))


def test_quantized_state_round_trips_with_its_draws():
    ts, ys = _rounded_series(50, seed=23)
    a = _model(quantum=1.0, seed=9)
    for t, y in zip(ts[:40], ys[:40], strict=False):
        a.update(t, y)
    b = ContinuousSSM.from_bytes(a.to_bytes())
    assert [a.update(t, y) for t, y in zip(ts[40:], ys[40:], strict=False)] == \
           [b.update(t, y) for t, y in zip(ts[40:], ys[40:], strict=False)]
