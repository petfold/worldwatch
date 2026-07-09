"""Layer-0 count flavor: PIT calibration on Poisson/NB, seasonality, bursts.

Acceptance (p0-implementation-plan §Key algorithms 4):
- PIT uniformity on synthetic Poisson / NB generators.
- Correct flagging of injected count bursts.
"""

import numpy as np
from scipy.stats import kstest

from worldwatch.layer0.count import NegBinomCount

HOUR = 3600
T0 = 1_700_000_000


def _times(n: int) -> list[int]:
    return [T0 + i * HOUR for i in range(n)]


def test_pit_uniform_poisson():
    rng = np.random.default_rng(1)
    ts = _times(3000)
    ys = rng.poisson(20.0, size=len(ts))
    m = NegBinomCount()
    qs = np.array([m.update(t, y) for t, y in zip(ts, ys, strict=True)][500:])
    p = kstest(qs, "uniform").pvalue
    assert p > 0.01, f"Poisson PIT not uniform: KS p={p:.4f}"


def test_pit_uniform_negative_binomial():
    """Overdispersed NB generator (mean 20, size 5) → model captures dispersion."""
    rng = np.random.default_rng(2)
    ts = _times(5000)
    ys = rng.negative_binomial(5, 5 / 25, size=len(ts))  # mean=20, size=5
    m = NegBinomCount()
    qs = np.array([m.update(t, y) for t, y in zip(ts, ys, strict=True)][1500:])
    p = kstest(qs, "uniform").pvalue
    assert p > 0.01, f"NB PIT not uniform: KS p={p:.4f}"


def test_overdispersion_estimated():
    rng = np.random.default_rng(3)
    ts = _times(5000)
    ys = rng.negative_binomial(5, 5 / 25, size=len(ts))  # true size ≈ 5
    m = NegBinomCount()
    for t, y in zip(ts, ys, strict=True):
        m.update(t, y)
    r = m._dispersion_size()
    # moment estimate is noisy; just assert it found meaningful overdispersion
    assert 1.5 < r < 20.0, f"dispersion size off: {r:.2f}"


def test_poisson_forced_model_miscalibrates_on_nb():
    """A near-Poisson fit (dispersion frozen high) fails KS on NB data,
    while the adaptive model passes — dispersion estimation earns its keep."""
    rng = np.random.default_rng(4)
    ts = _times(5000)
    ys = rng.negative_binomial(5, 5 / 25, size=len(ts))

    adaptive = NegBinomCount()
    frozen = NegBinomCount(disp_lr=0.0)  # never learns overdispersion → Poisson-like

    qa = np.array([adaptive.update(t, y) for t, y in zip(ts, ys, strict=True)][1500:])
    qf = np.array([frozen.update(t, y) for t, y in zip(ts, ys, strict=True)][1500:])

    assert kstest(qa, "uniform").pvalue > 0.01
    assert kstest(qf, "uniform").pvalue < 0.01


def test_seasonal_hour_profile_learned():
    rng = np.random.default_rng(5)
    ts = _times(6000)
    base = 30.0
    ys = []
    for t in ts:
        h = (t // HOUR) % 24
        factor = 1.0 + 0.6 * np.sin(2 * np.pi * h / 24)
        ys.append(rng.poisson(base * factor))
    m = NegBinomCount(seasonal_hour=True)
    qs = np.array([m.update(t, y) for t, y in zip(ts, ys, strict=True)][2000:])
    p = kstest(qs, "uniform").pvalue
    assert p > 0.01, f"seasonal PIT not uniform: KS p={p:.4f}"
    # learned hour profile should peak near the generator's peak (h=6)
    assert 3 <= int(np.argmax(m._f_hour)) <= 9


def test_burst_is_flagged():
    rng = np.random.default_rng(6)
    ts = _times(600)
    ys = list(rng.poisson(10.0, size=len(ts)))
    burst_i = 500
    ys[burst_i] = 100  # 10x spike

    m = NegBinomCount()
    qs = [m.update(t, y) for t, y in zip(ts, ys, strict=True)]

    assert qs[burst_i] > 0.99, f"burst not flagged: q={qs[burst_i]:.4f}"
    # steady-state points rarely hit the extreme tail
    tail = np.mean([q > 0.99 for q in qs[100:490]])
    assert tail < 0.05


def test_serialization_roundtrip():
    rng = np.random.default_rng(7)
    ts = _times(1000)
    ys = rng.poisson(15.0, size=len(ts))
    m = NegBinomCount(seasonal_hour=True)
    for t, y in zip(ts, ys, strict=True):
        m.update(t, y)

    r = NegBinomCount.from_bytes(m.to_bytes())
    assert r._mu == m._mu
    assert r._s2 == m._s2
    assert r._mbar == m._mbar
    assert np.allclose(r._f_hour, m._f_hour)
    assert r._dispersion_size() == m._dispersion_size()


def test_first_observation_returns_half():
    m = NegBinomCount()
    assert m.update(T0, 10) == 0.5
    assert m.rate > 0
