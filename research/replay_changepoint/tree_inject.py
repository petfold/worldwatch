"""Detection delay on injected swarms: Layer 0 today against the pooled tree (ADR 0005).

Into the USGS replay's real 5-minute counts, swarms are injected: about one extra event an hour
for 6 hours (Poisson), in single cells of each sparsity class, and spread over all the cells
with events of a resolution-2 region. Both models score the injected and the clean data (the
vectorized BayesianCount of tree_layer0.py, the tree with a 3-day forgetting time); the delay
is the time from a swarm's onset to its cells' first alarm (q_detect >= the threshold), and the
false alarms are the clean run's alarms in the same cells. Also at matched false-alarm rates:
the tree's threshold set so that its clean alarms in these cells equal today's at 0.999.

    .venv/bin/python research/replay_changepoint/tree_inject.py     # ~15 min on 4 cores
"""

from __future__ import annotations

import os

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import math
import sys
from multiprocessing import Pool

import h3
import numpy as np
from scipy.special import gammaln

sys.path.insert(0, os.path.dirname(__file__))
import tree_layer0 as tl  # noqa: E402

RATE = 1 / 12  # extra events per window: one an hour
DURATION = 72  # windows: 6 hours
AFTER = 288  # windows after the swarm's end still counted as its detection
N_PER_CLASS = 12
N_REGIONS = 10
MEMORY = 3 * 86400
G: dict | None = None
COUNTS: dict[str, np.ndarray] = {}  # the clean and injected counts, shared with the workers by fork


def run(args):
    """One group of base cells' subtrees over all windows, for counts Y: per-window q_detect of
    the recorded cells under Layer 0 today and under the tree."""
    roots, name, record = args
    assert G is not None
    Y_all = COUNTS[name]
    sel = np.flatnonzero(np.isin(G["root"], roots))
    local = {g: i for i, g in enumerate(sel)}
    n = len(sel)
    Y = Y_all[sel].astype(np.int64)
    res, areas, n_empty = G["res"][sel], G["areas"][sel], G["n_empty"][sel]
    parent = np.array([local[p] if p >= 0 else -1 for p in G["parent"][sel]])
    rec = [local[g] for g in record if g in local]
    paths = []
    for i in rec:
        row, j = [0] * 4, i
        for r in range(3, -1, -1):
            row[r] = j
            j = parent[j]
        paths.append(row)
    paths = np.array(paths, int).reshape(-1, 4)
    share = areas[paths[:, 3]][:, None] / areas[paths] if len(rec) else np.zeros((0, 4))
    m = tl.Batch(n + 1)
    m.start(np.append(Y[:, 0], 0))
    corr = gammaln(Y + 1.0) - Y * np.log(areas)[:, None]
    S = np.zeros(n + 1)
    dpool = math.exp(-300 / MEMORY)
    lr, l1r = math.log(tl.RHO), math.log1p(-tl.RHO)
    levels = [np.flatnonzero(res == r) for r in range(4)]
    T = Y.shape[1]
    out = np.full((2, len(rec), T), np.nan)
    for t in range(1, T):
        a, b = m.discounted()
        lam = m.lam(a, b)
        w = m.w
        ynode = np.append(Y[:, t], 0)
        pmf, _ = tl.Batch.predictive(ynode, np.ones(n + 1), a, b, lam)
        inc = np.log(np.clip((w * pmf).sum(axis=1), 1e-300, None))
        inc[:n] += corr[:, t]
        S = S * dpool + inc
        Sn, Se = S[:n], S[n]
        Z = np.zeros(n)
        split = n_empty * Se
        for r in range(3, -1, -1):
            ii = levels[r]
            Z[ii] = Sn[ii] if r == 3 else np.logaddexp(lr + Sn[ii], l1r + split[ii])
            if r > 0:
                np.add.at(split, parent[ii], Z[ii])
        pleaf = np.where(res == 3, 1.0, np.exp(np.clip(lr + Sn - Z, -700, 0)))
        reach = np.zeros(n)
        for r in range(4):
            ii = levels[r]
            reach[ii] = 1.0 if r == 0 else reach[parent[ii]] * (1 - pleaf[parent[ii]])
        pbin = reach * pleaf
        if len(rec):
            flat = paths.ravel()
            yy = np.repeat(Y[paths[:, 3], t], 4)
            pm, bl = tl.Batch.predictive(yy, share.ravel(), a[flat], b[flat], lam[flat])
            p = (w[flat] * pm).sum(axis=1).reshape(-1, 4)
            lo = (w[flat] * bl).sum(axis=1).reshape(-1, 4)
            for v, wv in enumerate([np.tile(np.eye(4)[3], (len(rec), 1)), pbin[paths]]):
                pv, lv = (wv * p).sum(axis=1), (wv * lo).sum(axis=1)
                out[v, :, t] = tl.conservative(lv, np.minimum(1.0, lv + pv))
        m.update(ynode, a, b, pmf)
    return [sel[i] for i in rec], out


def main():
    global G
    G = tl.build()
    nodes, res, T = G["nodes"], G["res"], G["T"]
    index = {c: i for i, c in enumerate(nodes)}
    cells = np.flatnonzero(res == 3)
    events = G["Y"][cells].sum(axis=1)
    rng = np.random.default_rng(7)
    classes = {"3-30 events": (3, 30), "31-299 events": (31, 299), "busy (300+)": (300, 10**9)}
    swarms = []  # (label, [cell node indices], onset)
    used_r1 = set()
    for label, (lo, hi) in classes.items():
        pool = [int(cells[k]) for k in rng.permutation(len(cells)) if lo <= events[k] <= hi]
        chosen = []
        for c in pool:
            r1 = h3.cell_to_parent(nodes[c], 1)
            if r1 not in used_r1:
                used_r1.add(r1)
                chosen.append(c)
            if len(chosen) == N_PER_CLASS:
                break
        swarms += [(label, [c], int(rng.integers(7 * 288, T - DURATION - AFTER))) for c in chosen]
    r2 = [i for i in np.flatnonzero(res == 2) if h3.cell_to_parent(nodes[i], 1) not in used_r1]
    r2 = [i for i in r2 if sum(G["parent"][c] == i for c in cells) >= 3]
    for i in rng.permutation(r2)[:N_REGIONS]:
        kids = [int(c) for c in cells if G["parent"][c] == i]
        used_r1.add(h3.cell_to_parent(nodes[i], 1))
        swarms.append(("a resolution-2 region", kids, int(rng.integers(7 * 288, T - DURATION - AFTER))))
    Y = G["Y"].astype(np.int32)
    Yi = Y.copy()
    for _, kids, t0 in swarms:
        extra = rng.poisson(RATE / len(kids), (len(kids), DURATION))
        for c, x in zip(kids, extra, strict=True):
            for r in range(4):
                node = index[h3.cell_to_parent(nodes[c], r) if r < 3 else nodes[c]]
                Yi[node, t0:t0 + DURATION] += x
    record = sorted({c for _, kids, _ in swarms for c in kids})
    roots = sorted({int(G["root"][c]) for c in record})
    groups = [[r] for r in roots]
    print(f"{len(swarms)} swarms in {len(record)} cells under {len(roots)} base cells", flush=True)
    results = {}
    COUNTS.update(clean=Y, injected=Yi)
    with Pool(int(os.environ.get("TREE_WORKERS", "4"))) as p:
        for name in ("clean", "injected"):
            out = p.map(run, [(g, name, record) for g in groups])
            rows = {}
            for ids, arr in out:
                for k, c in enumerate(ids):
                    rows[c] = arr[:, k]
            results[name] = rows
            print(f"  {name} done", flush=True)
    days = (T - tl.WARMUP) / 288
    clean = results["clean"]

    def alarms(v, thr):
        return sum(np.nansum(clean[c][v][tl.WARMUP:] >= thr) for c in record)

    base = alarms(0, 0.999)
    thr = 0.999  # the tree's threshold matched to today's false alarms, by bisection on log(1 - q)
    if alarms(1, thr) > base:
        lo_, hi_ = 0.999, 1 - 1e-9
        for _ in range(40):
            mid = 1 - math.sqrt((1 - lo_) * (1 - hi_))
            if alarms(1, mid) > base:
                lo_ = mid
            else:
                hi_ = mid
        thr = hi_
    print(f"\nclean alarms in these {len(record)} cells over {days:.0f} days: Layer 0 today {base}, "
          f"tree {alarms(1, 0.999)}; tree's threshold at today's false alarms: 1 - {1 - thr:.2e}\n")
    print("| swarm in | model | detected | median delay (h) | within the 6 h |")
    print("|---|---|---|---|---|")
    for label in [*classes, "a resolution-2 region"]:
        sw = [s for s in swarms if s[0] == label]
        for v, name, th in ((0, "Layer 0 today", 0.999), (1, "tree", 0.999), (1, "tree, matched", thr)):
            delays = []
            for _, kids, t0 in sw:
                first = [np.flatnonzero(results["injected"][c][v][t0:t0 + DURATION + AFTER] >= th) for c in kids]
                hits = [f[0] for f in first if len(f)]
                delays.append(min(hits) * 5 / 60 if hits else np.nan)
            d = np.array(delays)
            ok = ~np.isnan(d)
            print(f"| {label} | {name} | {ok.sum()} of {len(sw)} | "
                  f"{np.median(d[ok]) if ok.any() else float('nan'):.1f} | {np.sum(d[ok] <= 6)} |")


if __name__ == "__main__":
    main()
