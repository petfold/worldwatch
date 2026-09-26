"""Layer-0 count flavor: a Bayesian dynamic Gamma–Poisson with unknown burstiness.

Acceptance (p0-implementation-plan §Key algorithms 4, guardrail 5):
- PIT uniformity on Poisson / negative-binomial / very bursty generators.
- Burstiness is inferred, and a model denied it is detectably miscalibrated.
- The model knows its uncertainty: little data → wide predictive (a jump is
  unremarkable), ample data → the same jump is flagged. No warm-up special case.
"""

import math

import numpy as np
from scipy.stats import kstest

from worldwatch.layer0.count import BayesianCount

HOUR = 3600
T0 = 1_700_000_000


def _run(ys, **kw):
    m = BayesianCount(**kw)
    return np.array([m.update(T0 + i * HOUR, y) for i, y in enumerate(ys)]), m


def _tails(qs):
    return float(np.mean((qs < 0.01) | (qs > 0.99)))


def test_pit_uniform_poisson():
    qs, m = _run(np.random.default_rng(1).poisson(20.0, 3000))
    assert kstest(qs[50:], "uniform").pvalue > 0.01
    assert math.isinf(m.dispersion_size())  # found no burstiness


def test_pit_uniform_negative_binomial():
    ys = np.random.default_rng(2).negative_binomial(5, 5 / 25, 5000)  # mean 20, size 5
    qs, m = _run(ys)
    assert kstest(qs[50:], "uniform").pvalue > 0.01
    assert 2.0 <= m.dispersion_size() <= 8.0


def test_pit_uniform_very_bursty_low_count():
    ys = np.random.default_rng(3).negative_binomial(0.5, 0.5 / 2.5, 5000)  # mean 2, size 0.5
    qs, _ = _run(ys)
    assert kstest(qs[50:], "uniform").pvalue > 0.01


def test_model_denied_burstiness_is_miscalibrated():
    """Calibration checks must detect a generator/model mismatch."""
    ys = np.random.default_rng(4).negative_binomial(2, 2 / 22, 4000)  # mean 20, size 2
    qs, _ = _run(ys, k_grid=(math.inf,))  # Poisson only
    assert kstest(qs[50:], "uniform").pvalue < 0.01
    assert _tails(qs[50:]) > 0.05


def test_few_bins_leave_a_jump_unremarkable():
    """The case that prompted this model: GDELT counts 1, 2, then 5."""
    qs, _ = _run([1, 2, 5])
    assert qs[-1] < 0.99  # Bayesian predictive ≈ 1-in-15, not 1-in-275


def test_same_jump_flagged_once_the_rate_is_known():
    rng = np.random.default_rng(5)
    qs, _ = _run(list(rng.poisson(1.5, 300)) + [12])
    assert qs[-1] > 0.99


def test_cold_start_is_never_overconfident():
    """Fresh models across a wide range of true rates: the first bins must not
    land in the tails more often than calibration allows."""
    rng = np.random.default_rng(6)
    early = []
    for _ in range(600):
        lam = math.exp(rng.uniform(math.log(0.3), math.log(300)))
        qs, _ = _run(rng.poisson(lam, 7))
        early.extend(qs[1:])
    assert _tails(np.array(early)) <= 0.02


def test_seasonal_hour_profile_learned():
    rng = np.random.default_rng(7)
    ys = [rng.poisson(30.0 * (1.0 + 0.6 * np.sin(2 * np.pi * (i % 24) / 24))) for i in range(4000)]
    qs, m = _run(ys, seasonal_hour=True)
    assert kstest(qs[1500:], "uniform").pvalue > 0.01
    peak = int(np.argmax(m._f_hour))
    assert 3 <= (peak - (T0 // HOUR) % 24) % 24 <= 9  # generator peaks 6 h into the cycle


def test_burst_is_flagged():
    rng = np.random.default_rng(8)
    ys = list(rng.poisson(10.0, 600))
    ys[500] = 100
    qs, _ = _run(ys)
    assert qs[500] > 0.99
    assert _tails(qs[100:490]) < 0.05


def test_serialization_roundtrip_continues_identically():
    rng = np.random.default_rng(9)
    ys = list(rng.poisson(15.0, 400))
    a = BayesianCount(seasonal_hour=True)
    for i, y in enumerate(ys[:300]):
        a.update(T0 + i * HOUR, y)
    b = BayesianCount.from_bytes(a.to_bytes())
    for i, y in enumerate(ys[300:], start=300):
        assert a.update(T0 + i * HOUR, y) == b.update(T0 + i * HOUR, y)


def test_first_observation_returns_half():
    m = BayesianCount()
    assert m.update(T0, 10) == 0.5
    assert m.rate > 0


def test_quiet_zeros_never_look_surprising_to_detection():
    # a rare event stream: mostly zeros. The randomized PIT of a zero is
    # u·P(0) and lands in the low 1% tail about 1% of the time; the detection
    # q of a zero is F(0) or 0.5 — never extreme when zero is the likeliest
    rng = np.random.default_rng(10)
    m = BayesianCount()
    pits, detect = [], []
    for i, y in enumerate(rng.poisson(0.05, 3000)):
        pits.append(m.update(T0 + i * HOUR, y))
        if y == 0:
            detect.append(m.last_detect_q)
    assert min(pits[200:]) < 0.01  # the random draw does reach the tail
    assert min(detect[200:]) >= 0.5


def test_detection_q_keeps_a_burst_extreme():
    rng = np.random.default_rng(8)
    ys = list(rng.poisson(10.0, 600))
    ys[500] = 100
    m = BayesianCount()
    for i, y in enumerate(ys):
        m.update(T0 + i * HOUR, y)
        if i == 500:
            assert m.last_detect_q > 0.999


def test_conservative_q_is_the_attainable_value_nearest_one_half():
    from worldwatch.layer0.count import conservative_q

    assert conservative_q(0.0, 0.97) == 0.5  # interval straddles the middle
    assert conservative_q(0.999, 1.0) == 0.999  # "at least this many"
    assert conservative_q(0.0, 0.002) == 0.002  # "at most this many"
