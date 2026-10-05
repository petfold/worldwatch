"""Replay real histories through Layer 0's continuous model; judge the PITs as
the nursery does (TV of the decile histogram, 1% tail ratios).

  python replay.py            the current model and the candidate fixes
Variants:
  current    ContinuousSSM as deployed: process noise in absolute units
  relative   process noise in units of the learned noise variance S (level_var,
             trend_var, seasonal_var become signal-to-noise ratios per time unit)
"""
import csv
import math
import pathlib
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))
from worldwatch.config.loader import load_sources  # noqa: E402
from worldwatch.layer0.continuous import ContinuousSSM  # noqa: E402
from worldwatch.layer0.models import make_model  # noqa: E402

WARMUP = 2 * 86400
SOURCES = load_sources(pathlib.Path(__file__).resolve().parents[2] / "src/worldwatch/config/sources")
DATA = {"btc": "btc_usd", "eth": "eth_usd", "rivers": "pegelonline_water", "elexon": "elexon_frequency"}


class Relative(ContinuousSSM):
    def _process_noise(self, dt):
        return super()._process_noise(dt) * self._S


def series(name):
    by = defaultdict(list)
    with open(f"data/{name}.csv") as f:
        for r in csv.DictReader(f):
            by[r["cell"]].append((int(r["ts"]), float(r["value"])))
    return {c: sorted(v) for c, v in by.items()}


def judge(q):
    q = np.asarray(q)
    n = len(q)
    dec = np.histogram(q, bins=10, range=(0, 1))[0]
    tv = 0.5 * np.abs(dec / n - 0.1).sum()
    return n, tv, (q <= 0.01).mean() / 0.01, (q >= 0.99).mean() / 0.01


def run(name, variant, **override):
    cfg = SOURCES[DATA[name]]
    if override:
        import dataclasses
        cfg = dataclasses.replace(cfg, extra={**cfg.extra, "model": {**cfg.extra.get("model", {}), **override}})
    qs = []
    for pts in series(name).values():
        m = make_model(cfg)
        if variant == "relative":
            m.__class__ = Relative
        t0 = pts[0][0]
        for ts, v in pts:
            q = m.update(ts, v)
            if ts - t0 >= WARMUP:
                qs.append(q)
    return judge(qs)


def show(name, variant, res, note=""):
    n, tv, lo, hi = res
    print(f"{name:7s} {variant:9s} {note:28s} n={n:6d} TV={tv:.3f} lo={lo:5.2f}x hi={hi:5.2f}x")


if __name__ == "__main__":
    for name in DATA:
        for variant in ("current", "relative"):
            show(name, variant, run(name, variant))


def converted(name):
    """The stanza's absolute noise settings as signal-to-noise ratios at its obs_scale."""
    mp = {**SOURCES[DATA[name]].extra.get("model", {})}
    s2 = float(mp.get("obs_scale", 1.0)) ** 2
    return {k: float(mp.get(k, d)) / s2 for k, d in (("level_var", 1e-2), ("trend_var", 1e-6), ("seasonal_var", 1e-4))}


class Quantized(Relative):
    """Relative noise, and a randomized PIT for values recorded to a quantum
    (whole cm, whole counts): q uniform between F at the cell's edges."""
    quantum = 1.0
    inverse = staticmethod(lambda y: y)
    forward = staticmethod(lambda x: x)
    rng = np.random.default_rng(0)

    def update(self, ts, y):
        if self._x is None:
            return super().update(ts, y)
        dt = max(1e-9, (ts - self._last_ts) / self.time_scale)
        x_pred, P_pred = self._predict(dt)
        z = self._z(ts)
        yhat = float(z @ x_pred)
        scale = math.sqrt(max(float(z @ P_pred @ z) + self._S, 1e-12))
        df = min(self._discounted_dof(ts), self.obs_dof)
        from scipy.stats import t as st
        raw = self.inverse(y)
        lo = st.cdf((self.forward(raw - self.quantum / 2) - yhat) / scale, df)
        hi = st.cdf((self.forward(raw + self.quantum / 2) - yhat) / scale, df)
        super().update(ts, y)
        return float(lo + (hi - lo) * self.rng.uniform())


def run_q(name, quantum=1.0, transform="", **override):
    import dataclasses
    cfg = SOURCES[DATA[name]]
    cfg = dataclasses.replace(cfg, extra={**cfg.extra, "model": {**cfg.extra.get("model", {}), **override}})
    qs = []
    for pts in series(name).values():
        m = make_model(cfg)
        m.__class__ = Quantized
        m.quantum = quantum
        if transform == "log1p":
            m.inverse, m.forward = staticmethod(math.expm1), staticmethod(lambda x: math.log1p(max(x, 0)))
        t0 = pts[0][0]
        for ts, v in pts:
            q = m.update(ts, v)
            if ts - t0 >= WARMUP:
                qs.append(q)
    return judge(qs)


PROPOSED = {  # stanza [model] changes under model v3
    "btc": {"scale_memory_seconds": 3600, "obs_dof": 3.0},
    "eth": {"scale_memory_seconds": 3600, "obs_dof": 3.0},
    "rivers": {"quantum": 1.0},
    "elexon": {},
}


def run_v3(name, **override):
    """The production model (v3) as built from the stanza plus `override`."""
    import dataclasses
    cfg = SOURCES[DATA[name]]
    cfg = dataclasses.replace(cfg, extra={**cfg.extra, "model": {**cfg.extra.get("model", {}), **override}})
    qs = []
    for cell, pts in series(name).items():
        m = make_model(cfg, cell)
        t0 = pts[0][0]
        for ts, v in pts:
            q = m.update(ts, v)
            if ts - t0 >= WARMUP:
                qs.append(q)
    return judge(qs)
