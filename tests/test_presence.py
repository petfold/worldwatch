"""Presence model: learned gaps are silent, novel silence is loud.

Acceptance (p0-implementation-plan §Key algorithms 5):
- scheduled nightly gap → no surprise
- novel 3-interval silence → presence_q in tail
"""

from worldwatch.layer0.presence import PresenceModel

HOUR = 3600
T0 = 1_700_000_000  # 00:00 UTC-aligned enough for slot bucketing


def _slot(day: int, hour: int) -> int:
    return T0 + day * 24 * HOUR + hour * HOUR


def test_learned_nightly_gap_is_not_surprising():
    """A source that reports 06:00–21:59 and is silent overnight, every day."""
    m = PresenceModel(lr=0.1)
    for day in range(40):
        for hour in range(24):
            reports = 6 <= hour < 22
            m.observe(_slot(day, hour), reported=reports)

    # a typical overnight silent slot after learning: barely surprising
    night_q = m.observe(_slot(40, 3), reported=False)
    assert night_q < 0.2, f"learned nightly gap flagged as surprising: q={night_q:.3f}"


def test_novel_daytime_silence_is_flagged():
    """A source that always reports, then goes silent for 3 slots."""
    m = PresenceModel(lr=0.1)
    for day in range(40):
        for hour in range(24):
            m.observe(_slot(day, hour), reported=True)

    q1 = m.observe(_slot(40, 10), reported=False)
    q2 = m.observe(_slot(40, 11), reported=False)
    q3 = m.observe(_slot(40, 12), reported=False)

    assert q1 < q2 < q3, "silence surprise should grow with run length"
    assert q3 > 0.99, f"3-interval silence not in tail: q={q3:.4f}"


def test_reporting_resets_the_run():
    m = PresenceModel(lr=0.1)
    for day in range(30):
        for hour in range(24):
            m.observe(_slot(day, hour), reported=True)

    m.observe(_slot(30, 9), reported=False)
    m.observe(_slot(30, 10), reported=False)
    assert m.observe(_slot(30, 11), reported=True) == 0.0  # reported → no surprise
    # run reset: the next single miss is scored fresh, not as a length-3 run
    fresh = m.observe(_slot(30, 12), reported=False)
    assert fresh < 0.999


def test_contrast_night_vs_day_in_same_model():
    """Within one model trained on a day/night pattern, daytime silence is
    much louder than nighttime silence."""
    m = PresenceModel(lr=0.1)
    for day in range(50):
        for hour in range(24):
            m.observe(_slot(day, hour), reported=(6 <= hour < 22))

    night = m.observe(_slot(50, 3), reported=False)
    # reset run then probe a daytime miss
    m.observe(_slot(50, 10), reported=True)
    day_silence = m.observe(_slot(50, 11), reported=False)
    assert day_silence > night + 0.5


def test_serialization_roundtrip():
    m = PresenceModel(lr=0.1)
    for day in range(10):
        for hour in range(24):
            m.observe(_slot(day, hour), reported=(hour % 2 == 0))
    m.observe(_slot(10, 1), reported=False)

    r = PresenceModel.from_bytes(m.to_bytes())
    import numpy as np

    assert np.allclose(r._p_hour, m._p_hour)
    assert r._silence_logsurv == m._silence_logsurv
    # same next output
    assert r.observe(_slot(10, 2), reported=False) == m.observe(_slot(10, 2), reported=False)
