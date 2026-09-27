"""Replay Worldwatch's usgs_seismic count streams (prepare.py) through the current Layer-0
count model (BayesianCount, the stanza's defaults), a Poisson-only change-point model
(bayesbin.ChangePointStream.poisson) and one with a burst factor (ChangePointStream.
overdispersed: negative binomial segments averaged over a grid of dispersions), window by
window, scoring each window before updating.

    replay.py            # the current and Poisson-only models  -> replay_results.npz
    replay.py mixture    # the burst-factor model               -> replay_results_mixture.npz

Both give the randomized PIT (calibration) and the conservative q_detect of ADR 0003
(alarms: q_detect >= 0.999, the upper tail). Saves per-window results for report.py.

    PYTHONPATH=src <python with bayesbin> research/replay_changepoint/replay.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

from bayesbin import ChangePointStream
from worldwatch.layer0.count import BayesianCount, conservative_q

CACHE = Path.home() / ".cache" / "worldwatch-research"
WARMUP = 2 * 288  # two days of 5-minute windows: sets the change-point prior, then scoring counts
RUN = 7 * 288  # the change-point model's expected segment length: a week


def replay_stream(counts: np.ndarray, t0: int, width: int, seed: int):
    n = len(counts)
    out = {k: np.full(n, np.nan) for k in ("q_cur", "d_cur", "m_cur", "q_cp", "d_cp", "m_cp", "run_cp", "pc_cp")}
    cur = BayesianCount(seed=seed)
    mean = max(counts[:WARMUP].mean(), 0.5 / WARMUP)
    cp = ChangePointStream.poisson(alpha=1.0, beta=1.0 / mean, expected_run_length=RUN)
    rng = np.random.default_rng(seed)
    for i, y in enumerate(counts):
        ts = t0 + i * width
        # the current model: its predictive mean before the update (discounted posterior)
        out["m_cur"][i] = cur.rate if cur._a is not None else np.nan
        out["q_cur"][i] = cur.update(ts, float(y))
        out["d_cur"][i] = cur.last_detect_q
        # the change-point model: score, then update
        below = float(cp.next_cdf(y - 1)[0]) if y >= 1 else 0.0
        at = float(cp.next_cdf(y)[0])
        out["q_cp"][i] = below + rng.random() * (at - below)
        out["d_cp"][i] = conservative_q(below, at)
        out["m_cp"][i] = cp.rate_now()[0] if cp.T else np.nan
        cp.update(y)
        lo, hi, p = cp.run_length_ranges()
        out["run_cp"][i] = float(p @ ((lo + hi) / 2))  # the posterior mean run length: the averaging window
        out["pc_cp"][i] = cp.p_change_within(12)  # P(a change in the last hour)
    return out


def replay_mixture(counts: np.ndarray, seed: int):
    """The change-point model with a burst factor, scored as replay_stream scores the others."""
    n = len(counts)
    out = {k: np.full(n, np.nan) for k in ("q_mx", "d_mx", "m_mx", "r_mx")}
    mean = max(counts[:WARMUP].mean(), 0.5 / WARMUP)
    mx = ChangePointStream.overdispersed(alpha=1.0, beta=1.0 / mean, expected_run_length=RUN)
    rng = np.random.default_rng(seed)
    for i, y in enumerate(counts):
        below = float(mx.next_cdf(y - 1)[0]) if y >= 1 else 0.0
        at = float(mx.next_cdf(y)[0])
        out["q_mx"][i] = below + rng.random() * (at - below)
        out["d_mx"][i] = conservative_q(below, at)
        out["m_mx"][i] = mx.rate_now()[0] if mx.T else np.nan
        mx.update(y)
        r, w = mx.dispersion_posterior()
        out["r_mx"][i] = r[np.argmax(w)]  # the most probable dispersion
    return out


def main() -> None:
    d = np.load(CACHE / "usgs_streams.npz")
    mixture = sys.argv[1:] == ["mixture"]
    res = {}
    for j, (name, counts) in enumerate(zip(d["names"], d["counts"])):
        t = time.perf_counter()
        res[str(name)] = (replay_mixture(counts, seed=2000 + j) if mixture
                          else replay_stream(counts, int(d["t0"]), int(d["width"]), seed=1000 + j))
        print(f"{name}: {time.perf_counter() - t:.0f} s", flush=True)
    out = CACHE / ("replay_results_mixture.npz" if mixture else "replay_results.npz")
    np.savez(out, **{f"{nm}|{k}": v for nm, r in res.items() for k, v in r.items()})


if __name__ == "__main__":
    main()
