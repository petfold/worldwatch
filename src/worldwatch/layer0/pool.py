"""Pooling a count stream's cells over the H3 tree (ADR 0005).

Layer 0 models each (stream, cell) on its own. A sparse cell's model then stays wide: a single
real event in a cell that sees one a month rarely stands out, and the cell's upper tail holds
too little mass. Pooled, every node of the tree above the stream's live cells (each cell and its
ancestors up to the coarsest resolution) runs the count model on its region's summed counts.
After each window the recursion

    Z(ν) = ρ S(ν) + (1 − ρ) Π_children Z(χ),     Z = S at a cell,

on the nodes' log predictive scores S (summed with a forgetting time) gives P(ν is a cell's bin |
recent windows), and the cell's predictive is the mixture of its own and its ancestors'
predictives, each at the cell's share of the ancestor's area. A child region with nothing live
in it counts as an empty region. Busy cells keep their own model; sparse ones borrow their
parents' rates. On the USGS replay (research/replay_changepoint/tree_layer0.py) the sparse
cells' upper tail came from half its nominal mass to nominal and the M >= 5 quakes alarmed in
their window from 58% to 72%, with the busy cells unchanged.

The recursions are BayesianCount's (layer0/count.py), vectorized over the nodes; the gamma
quantiles of its quadrature come from a table interpolated in log shape (relative error about
1e-7). Every node's model is a BayesianCount, and a cell's is the one Layer 0 keeps for it
anyway, so switching pooling off carries on from the same states.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import h3
import numpy as np
from scipy.special import betainc, gammaincinv, gammaln

from worldwatch.layer0.count import K_GRID, MODEL_VERSION, BayesianCount

POOL_SCALE = -2  # model_state rows of the pool: the ancestors' models and every node's score
POOL_STATE_VERSION = 1
POOLED_VERSION = 100 + MODEL_VERSION  # surprise.model_version of a pooled q
EMPTY = "empty"  # the empty region's model (its cell key in model_state)

_EPS = 1e-12
_N_Q = 24  # BayesianCount's quantile nodes
_U = (np.arange(_N_Q) + 0.5) / _N_Q
_K = np.array([k for k in K_GRID if not math.isinf(k)])
# the standard gamma's quantiles at _U, tabulated in log shape
_LA = np.linspace(math.log(1e-2), math.log(1e7), 8001)
_LQ = np.log(np.maximum(gammaincinv(np.exp(_LA)[:, None], _U[None, :]), 1e-300))
_DLA = float(_LA[1] - _LA[0])


@dataclass(frozen=True)
class PoolSettings:
    rho: float = 0.1  # P(a node is one bin), a priori
    memory_seconds: float = 3 * 86400.0  # the forgetting time of the nodes' scores
    coarsest: int = 0  # the coarsest resolution pooled over

    @classmethod
    def from_model_table(cls, mp: dict[str, Any]) -> PoolSettings | None:
        """From a stanza's [model] table: `pool = "h3"` switches pooling on."""
        kind = mp.get("pool")
        if kind is None:
            return None
        if kind != "h3":
            raise ValueError(f"[model] pool = {kind!r}: only 'h3' is known")
        return cls(
            rho=float(mp.get("pool_rho", 0.1)),
            memory_seconds=float(mp.get("pool_memory_seconds", 3 * 86400.0)),
            coarsest=int(mp.get("pool_coarsest", 0)),
        )


def _quantiles(a: np.ndarray) -> np.ndarray:
    """The standard gamma's quantiles at _U for an array of shapes a: shape a.shape + (_N_Q,)."""
    x = (np.log(a) - _LA[0]) / _DLA
    i = np.clip(x.astype(np.int64), 0, len(_LA) - 2)
    f = np.clip(x - i, 0.0, 1.0)[..., None]
    return np.asarray(np.exp((1 - f) * _LQ[i] + f * _LQ[i + 1]))


def predictive(
    y: np.ndarray, e: np.ndarray, a: np.ndarray, b: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """BayesianCount._predictive for many rows at once: per hypothesis P(Y = y) and P(Y < y) at
    exposure e, for rows y, e (n,) and a, b (n, K) (the Poisson hypothesis last)."""
    n, nk = a.shape
    pmf = np.empty((n, nk))
    below = np.zeros((n, nk))
    k = _K[None, :, None]
    lam = _quantiles(a[:, :-1]) / b[:, :-1, None]
    p = k / (k + np.maximum(lam * e[:, None, None], _EPS))
    pp = b[:, -1] / (b[:, -1] + e)
    zero = y == 0
    if zero.any():
        pmf[zero, :-1] = np.exp(k[0] * np.log(p[zero])).mean(axis=2)
        pmf[zero, -1] = np.exp(a[zero, -1] * np.log(pp[zero]))
    nz = ~zero
    if nz.any():
        yy = y[nz].astype(float)[:, None, None]
        pz = p[nz]
        with np.errstate(divide="ignore"):
            lp = gammaln(yy + k) - gammaln(k) - gammaln(yy + 1) + k * np.log(pz) + yy * np.log1p(-pz)
        pmf[nz, :-1] = np.exp(lp).mean(axis=2)
        below[nz, :-1] = betainc(k, yy, pz).mean(axis=2)
        y1, a1, q1 = y[nz].astype(float), a[nz, -1], pp[nz]
        with np.errstate(divide="ignore"):
            pmf[nz, -1] = np.exp(
                gammaln(y1 + a1) - gammaln(a1) - gammaln(y1 + 1) + a1 * np.log(q1) + y1 * np.log1p(-q1)
            )
        below[nz, -1] = betainc(a1, y1, q1)
    return pmf, below


def _arr(x: np.ndarray | None) -> np.ndarray:
    assert x is not None
    return x


class TreePool:
    """One count stream's pool: the ancestors' models, every node's score, and the window step.

    `cell_model(cell)` gives Layer 0's own model of a cell (kept and saved by the caller);
    `new_model(node)` cold-starts a model for an ancestor or the empty region; `saved(node)` is
    the node's saved pool state, if any."""

    def __init__(
        self,
        settings: PoolSettings,
        cell_model: Callable[[str], BayesianCount],
        new_model: Callable[[str], BayesianCount],
        saved: Callable[[str], bytes | None] = lambda node: None,
    ) -> None:
        self.settings = settings
        self._cell_model = cell_model
        self._new_model = new_model
        self._saved = saved
        self.models: dict[str, BayesianCount] = {}  # ancestors and the empty region
        self.scores: dict[str, tuple[float, int | None]] = {}  # node -> (S, ts of its last window)
        self._area: dict[str, float] = {}
        self._children: dict[str, list[str]] = {}
        self.res: int | None = None
        self.last_weights: dict[str, dict[str, float]] = {}  # per cell, P(node is its bin), last window
        self._plan_key: tuple[str, ...] | None = None
        self._plan_cache: dict[str, Any] | None = None
        self._load(EMPTY)

    # --- nodes -------------------------------------------------------------------------------

    def _load(self, node: str) -> None:
        blob = self._saved(node)
        if blob is not None:
            p = json.loads(blob)
            if p.get("v") == POOL_STATE_VERSION:
                self.scores[node] = (float(p["S"]), p["ts"])
                if p.get("model") is not None:
                    self.models[node] = BayesianCount.from_bytes(p["model"].encode())
                return
        if node == EMPTY or not self._is_cell(node):
            self.models[node] = self._new_model(node)
        start = self.scores.get(EMPTY, (0.0, None))[0]  # a region new to the pool was empty so far
        self.scores[node] = (start if node != EMPTY else 0.0, None)

    def _is_cell(self, node: str) -> bool:
        return self.res is not None and node != EMPTY and h3.get_resolution(node) == self.res

    def model(self, node: str) -> BayesianCount:
        return self._cell_model(node) if self._is_cell(node) else self.models[node]

    def ancestors(self, cell: str) -> list[str]:
        r = h3.get_resolution(cell)
        return [h3.cell_to_parent(cell, k) for k in range(r - 1, self.settings.coarsest - 1, -1)]

    def area(self, node: str) -> float:
        """A region's area: the sum of its cells' (km²), exact to 4 resolutions below."""
        if node not in self._area:
            r = h3.get_resolution(node)
            assert self.res is not None
            if r == self.res or self.res - r > 4:
                self._area[node] = float(h3.cell_area(node, "km^2"))
            else:
                self._area[node] = float(sum(h3.cell_area(c, "km^2") for c in h3.cell_to_children(node, self.res)))
        return self._area[node]

    def children(self, node: str) -> list[str]:
        if node not in self._children:
            self._children[node] = list(h3.cell_to_children(node, h3.get_resolution(node) + 1))
        return self._children[node]

    def states(self, nodes: list[str]) -> list[tuple[str, bytes]]:
        """The pool states of `nodes` (and the empty region) for model_state at POOL_SCALE."""
        out = []
        for node in [*nodes, EMPTY]:
            s, ts = self.scores[node]
            m = self.models.get(node)
            payload = {"v": POOL_STATE_VERSION, "S": s, "ts": ts,
                       "model": None if m is None else m.to_bytes().decode()}
            out.append((node, json.dumps(payload, separators=(",", ":")).encode()))
        return out

    # --- one window --------------------------------------------------------------------------

    def _plan(self, live: list[str]) -> dict[str, Any]:
        """The tree over the live cells: nodes finest first, their parents, levels, areas, each
        cell's path (itself, then its ancestors) and its share of each. Kept while `live` holds."""
        key = tuple(live)
        if self._plan_key == key and self._plan_cache is not None:
            return self._plan_cache
        nodes: set[str] = set(live)
        for c in live:
            nodes.update(self.ancestors(c))
        order = sorted(nodes, key=lambda c: (-h3.get_resolution(c), c))
        for node in order:
            if node not in self.scores:
                self._load(node)
        idx = {c: i for i, c in enumerate(order)}
        res = np.array([h3.get_resolution(c) for c in order])
        coarsest = self.settings.coarsest
        parent = np.array([idx[h3.cell_to_parent(c, int(r) - 1)] if r > coarsest else -1
                           for c, r in zip(order, res, strict=True)])
        stepped = np.bincount(parent[parent >= 0], minlength=len(order))
        n_empty = np.array([0 if r == self.res else len(self.children(c)) for c, r in zip(order, res, strict=True)])
        n_empty = n_empty - stepped
        areas = np.array([self.area(c) for c in order])
        paths = np.array([[idx[c], *[idx[a] for a in self.ancestors(c)]] for c in live])
        plan = {
            "order": order, "res": res, "parent": parent, "n_empty": n_empty, "areas": areas,
            "levels": [(r, np.flatnonzero(res == r)) for r in range(int(res.max()), coarsest - 1, -1)],
            "paths": paths, "share": areas[paths[:, :1]] / areas[paths],
            "models": [self.model(c) for c in order] + [self.models[EMPTY]],
        }
        if any(m.k_grid != K_GRID for m in plan["models"]):
            raise ValueError("pooling needs the count model's default dispersion grid")
        self._plan_key, self._plan_cache = key, plan
        return plan

    def step(self, ts: int, counts: dict[str, int], live: list[str]) -> dict[str, tuple[float, float]]:
        """Score one window for the live cells (their counts in `counts`; absent = 0) and advance
        every node: {cell: (q, q_detect)}, the randomized PIT and the conservative q of ADR 0003."""
        if not live:
            return {}
        if self.res is None:
            self.res = h3.get_resolution(live[0])
        plan = self._plan(live)
        order, paths, models = plan["order"], plan["paths"], plan["models"]
        n, L = len(order), paths.shape[1]
        yc = np.array([int(counts.get(c, 0)) for c in live])
        y = np.zeros(n + 1, np.int64)  # the last row: the empty region
        np.add.at(y, paths.ravel(), np.repeat(yc, L))
        tmpl = models[0]
        seasonal = tmpl.seasonal_hour or tmpl.seasonal_dow
        e = np.array([m._exposure(ts) for m in models]) if seasonal else np.ones(n + 1)
        known = np.array([m._a is not None and m._b is not None and m._last_ts is not None for m in models])
        ki = np.flatnonzero(known)
        nk = len(K_GRID)
        a_d = np.zeros((n + 1, nk))
        b_d = np.zeros((n + 1, nk))
        delta = np.ones(n + 1)
        if ki.size:
            last = np.array([models[i]._last_ts for i in ki], dtype=float)
            delta[ki] = np.exp(-np.maximum(0.0, ts - last) / np.array([models[i].memory_seconds for i in ki]))
            a0 = np.array([models[i].prior_shape for i in ki])[:, None]
            b0 = np.array([models[i].prior_rate for i in ki])[:, None]
            a_d[ki] = delta[ki, None] * np.stack([_arr(models[i]._a) for i in ki]) + (1 - delta[ki, None]) * a0
            b_d[ki] = delta[ki, None] * np.stack([_arr(models[i]._b) for i in ki]) + (1 - delta[ki, None]) * b0
        logw = np.stack([_arr(m._logw) for m in models])
        w = np.exp(logw - np.logaddexp.reduce(logw, axis=1)[:, None])

        # each node's predictive of its own count: its score
        pmf_own = np.ones((n + 1, nk))
        inc = np.zeros(n + 1)
        if ki.size:
            pmf_own[ki], _ = predictive(y[ki], e[ki], a_d[ki], b_d[ki])
            inc[ki] = np.log(np.clip((w[ki] * pmf_own[ki]).sum(axis=1), 1e-300, None))
        yn = y[:n].astype(float)
        inc[:n] += np.where(known[:n], gammaln(yn + 1.0) - yn * np.log(plan["areas"]), 0.0)  # the allocation
        mem = self.settings.memory_seconds
        S = np.zeros(n + 1)
        for i, node in enumerate([*order, EMPTY]):
            sc, t_last = self.scores[node]
            keep = 1.0 if (t_last is None or math.isinf(mem)) else math.exp(-max(0, ts - t_last) / mem)
            S[i] = keep * sc + inc[i]
        s_empty = S[-1]

        # the tree's weights: Z up from the cells, then P(reached) and P(a bin) down from the top
        parent, n_empty = plan["parent"], plan["n_empty"]
        lr, l1r = math.log(self.settings.rho), math.log1p(-self.settings.rho)
        Z = np.zeros(n)
        split = n_empty * s_empty
        for r, ii in plan["levels"]:  # finest first
            Z[ii] = S[ii] if r == self.res else np.logaddexp(lr + S[ii], l1r + split[ii])
            if r > self.settings.coarsest:
                np.add.at(split, parent[ii], Z[ii])
        pleaf = np.where(plan["res"] == self.res, 1.0, np.exp(np.clip(lr + S[:n] - Z, -700.0, 0.0)))
        reach = np.zeros(n)
        for r, ii in reversed(plan["levels"]):  # coarsest first
            reach[ii] = 1.0 if r == self.settings.coarsest else reach[parent[ii]] * (1 - pleaf[parent[ii]])
        pbin = reach * pleaf

        # each live cell's predictive under each of its candidates (itself and its ancestors)
        flat = paths.ravel()
        ok = known[flat]
        p_c = np.zeros(flat.size)
        lo_c = np.zeros(flat.size)
        if ok.any():
            rows = flat[ok]
            pm, bl = predictive(np.repeat(yc, L)[ok], plan["share"].ravel()[ok] * e[rows], a_d[rows], b_d[rows])
            p_c[ok] = (w[rows] * pm).sum(axis=1)
            lo_c[ok] = (w[rows] * bl).sum(axis=1)
        wv = pbin[paths] * ok.reshape(paths.shape)
        tot = wv.sum(axis=1)
        wv = wv / np.where(tot > 0, tot, 1.0)[:, None]
        pv = (wv * p_c.reshape(paths.shape)).sum(axis=1)
        lv = (wv * lo_c.reshape(paths.shape)).sum(axis=1)
        self.last_weights = {c: {order[i]: float(x) for i, x in zip(paths[k], wv[k], strict=True)}
                             for k, c in enumerate(live) if tot[k] > 0}
        out: dict[str, tuple[float, float]] = {}
        for k, c in enumerate(live):
            if tot[k] <= 0:
                out[c] = (0.5, 0.5)  # no informative predictive yet (as BayesianCount's first bin)
                continue
            m = models[paths[k, 0]]
            # u as the cell's own model would draw it (none in its first bin), so that its random
            # sequence is the same with pooling on or off
            u = float(m._rng.random()) if m._rng is not None and known[paths[k, 0]] else 0.5
            q = min(1.0, max(0.0, float(lv[k] + u * pv[k])))
            hi = min(1.0, float(lv[k] + pv[k]))
            out[c] = (q, float(lv[k]) if lv[k] > 0.5 else (hi if hi < 0.5 else 0.5))

        # advance every node as BayesianCount.update would
        self._advance(models, ki, ~known, y, e, a_d, b_d, delta, logw, w, pmf_own, ts)
        for i, node in enumerate([*order, EMPTY]):
            self.scores[node] = (float(S[i]), ts)
        return out

    def _advance(self, models: list[BayesianCount], ki: np.ndarray, fresh: np.ndarray, y: np.ndarray,
                 e: np.ndarray, a_d: np.ndarray, b_d: np.ndarray, delta: np.ndarray, logw: np.ndarray,
                 w: np.ndarray, pmf: np.ndarray, ts: int) -> None:
        for i in np.flatnonzero(fresh):  # the first bin: no predictive, the prior plus the count
            m = models[i]
            nk = len(m.k_grid)
            m._a = np.full(nk, m.prior_shape + y[i], dtype=float)
            m._b = np.full(nk, m.prior_rate + e[i], dtype=float)
            m._last_ts = ts
            m.last_detect_q = 0.5
        if not ki.size:
            return
        lw = delta[ki, None] * logw[ki] + np.log(np.clip(pmf[ki], _EPS, None))
        lw -= np.logaddexp.reduce(lw, axis=1)[:, None]
        k = np.asarray(K_GRID, dtype=float)[None, :]
        lam_hat = a_d[ki] / b_d[ki]
        yk = y[ki][:, None].astype(float)
        with np.errstate(invalid="ignore"):
            e_omega = np.where(np.isinf(k), 1.0, (k + yk) / (k + lam_hat * e[ki, None]))
        a_new = a_d[ki] + yk
        b_new = b_d[ki] + e[ki, None] * e_omega
        m_hat = (w[ki] * lam_hat).sum(axis=1) * e[ki]
        for row, i in enumerate(ki):
            m = models[i]
            m._logw = lw[row]
            m._a = a_new[row]
            m._b = b_new[row]
            m._last_ts = ts
            m._update_seasonal(ts, float(y[i]), float(m_hat[row]))
