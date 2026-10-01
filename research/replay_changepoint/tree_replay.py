"""The H3 tree with a change-point stream per node, on the USGS replay (5-minute windows).

Each node of the H3 tree under the resolution-0 cells that hold the replay's 12 busiest cells
(their resolution-3 cells with events and the ancestors) runs bayesbin's ChangePointStream on
its region's summed counts, set up as replay.py sets up its streams: Poisson segments, prior
Gamma(1, 1/m) with m the node's mean count over the 2-day warm-up, expected segment length a
week. After every window the recursion

    Z(ν) = ρ Z_time(ν) + (1 − ρ) Π_children Z(χ)

over the streams' running evidences gives P(ν is a cell's bin | data so far), and the cell's
predictive for the next window is the mixture of its ancestors' predictives, each at the cell's
share of the ancestor's area. Scored as replay.py scores: the randomized PIT, the conservative
q of ADR 0003 (alarms: q >= 0.999) and the log predictive probability, after the warm-up.

Phase 1 (in parallel over nodes): each node's stream, its log predictive of its own count per
window, and its predictive of every scored cell's count in its region. Phase 2: the tree's
weights and the mixtures, for several ρ. The world itself is taken to split (it is far from one
rate), so the resolution-0 subtrees are independent.

    <python with bayesbin and h3> research/replay_changepoint/tree_replay.py      # ~35 min on 4 cores
    <python with bayesbin and h3> research/replay_changepoint/tree_replay.py report
"""

from __future__ import annotations

import os

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")  # one BLAS thread per worker process

import csv
import glob
import sys
import time
from datetime import UTC, datetime
from multiprocessing import Pool
from pathlib import Path

import h3
import numpy as np
from bayesbin import ChangePointStream
from scipy.special import gammaln
from scipy.stats import kstest

CACHE = Path.home() / ".cache" / "worldwatch-research"
WARMUP = 2 * 288
RUN = 7 * 288
ALARM = 0.999
RES = 3
RHOS = (0.1, 0.5)
N_SPARSE = 40  # sparse cells scored besides the replay's 12: 3-30 events, in the same subtrees
ALASKA = "8322c4fffffffff"
OUT = CACHE / ("tree_replay_phase1.npz" if "TREE_T" not in os.environ else "tree_replay_phase1_quick.npz")


def conservative_q(q_lo: float, q_hi: float) -> float:
    """ADR 0003 (worldwatch.layer0.count.conservative_q): the least extreme PIT consistent with
    a discrete observation whose attainable PIT interval is [q_lo, q_hi]."""
    if q_lo > 0.5:
        return q_lo
    if q_hi < 0.5:
        return q_hi
    return 0.5


def area(c: str) -> float:
    r = h3.get_resolution(c)
    return h3.cell_area(c, "km^2") if r == RES else sum(h3.cell_area(d, "km^2") for d in h3.cell_to_children(c, RES))


def build():
    """Windows, nodes, their counts, the scored cells and where each sits."""
    d = np.load(CACHE / "usgs_streams.npz")
    t0, width = int(d["t0"]), int(d["width"])
    T = int(os.environ.get("TREE_T", d["counts"].shape[1]))  # fewer windows: a quick check
    replay = [str(n) for n in d["names"]][1:]
    rows = []
    for f in sorted(glob.glob(str(CACHE / "usgs" / "*.csv"))):
        with open(f) as fh:
            for r in csv.DictReader(fh):
                ts = datetime.fromisoformat(r["time"].replace("Z", "+00:00")).timestamp()
                cell = h3.latlng_to_cell(float(r["latitude"]), float(r["longitude"]), RES)
                rows.append((int((ts - t0) // width), cell))
    roots = {h3.cell_to_parent(c, 0) for c in replay}
    cells = sorted({c for _, c in rows if h3.cell_to_parent(c, 0) in roots})
    nodes = sorted(set(cells) | {h3.cell_to_parent(c, r) for c in cells for r in range(RES)},
                   key=lambda c: (h3.get_resolution(c), c))
    index = {c: i for i, c in enumerate(nodes)}
    Y = np.zeros((len(nodes), T), np.int32)
    for w, c in rows:
        if c in index and 0 <= w < T:
            for r in range(RES + 1):
                Y[index[h3.cell_to_parent(c, r) if r < RES else c], w] += 1
    for j, c in enumerate(replay):  # the node series of the replay's cells are the replay's own
        assert np.array_equal(Y[index[c]], d["counts"][j + 1][:T]), c
    totals = {c: int(Y[index[c]].sum()) for c in cells}
    sparse = sorted(c for c in cells if 3 <= totals[c] <= 30 and c not in replay)
    rng = np.random.default_rng(0)
    sparse = sorted(rng.choice(sparse, size=min(N_SPARSE, len(sparse)), replace=False).tolist())
    scored = replay + sparse
    res = np.array([h3.get_resolution(c) for c in nodes])
    parent = np.array([index[h3.cell_to_parent(c, r - 1)] if r > 0 else -1 for c, r in zip(nodes, res, strict=True)])
    areas = np.array([area(c) for c in nodes])
    n_empty = np.array([0 if r == RES else sum(k not in index for k in h3.cell_to_children(c, r + 1))
                        for c, r in zip(nodes, res, strict=True)])
    paths = [[index[h3.cell_to_parent(c, r) if r < RES else c] for r in range(RES + 1)] for c in scored]
    return dict(t0=t0, width=width, T=T, nodes=nodes, Y=Y, res=res, parent=parent, areas=areas,
                n_empty=n_empty, scored=scored, n_replay=len(replay), paths=paths)


G = None  # the shared problem, inherited by the worker processes


def run_node(i: int):
    """Node i's stream over all windows: the log predictive of its own count per window, and
    (log p, F(y - 1), F(y)) of every scored cell in its region, each before the update."""
    Y = G["Y"][i]
    mean = max(Y[:WARMUP].mean(), 0.5 / WARMUP)
    cp = ChangePointStream.poisson(alpha=1.0, beta=1.0 / mean, expected_run_length=RUN)
    mine = [(k, G["areas"][G["paths"][k][RES]] / G["areas"][i]) for k, path in enumerate(G["paths"]) if i in path]
    ys = {k: G["Y"][G["paths"][k][RES]] for k, _ in mine}
    lp = np.zeros(G["T"])
    pred = {k: np.zeros((G["T"], 3)) for k, _ in mine}
    for t in range(G["T"]):
        for k, size in mine:
            y = int(ys[k][t])
            lg = float(cp.next_logpmf(y, size)[0])
            if y == 0:
                pred[k][t] = (lg, 0.0, np.exp(lg))
            else:
                lo, hi = cp.next_cdf([y - 1, y], size)
                pred[k][t] = (lg, lo, hi)
        before = cp.log_marginal
        cp.update(int(Y[t]))
        lp[t] = cp.log_marginal - before
    return i, lp, pred


def phase1():
    global G
    G = build()
    n = len(G["nodes"])
    work = [1 + sum(i in p for p in G["paths"]) for i in range(n)]
    order = sorted(range(n), key=lambda i: -work[i])  # the slowest first
    print(f"{n} nodes, {G['T']} windows, {len(G['scored'])} scored cells ({G['n_replay']} from the replay)", flush=True)
    tic = time.perf_counter()
    lp = np.zeros((n, G["T"]))
    pred = {}
    with Pool(int(os.environ.get("TREE_WORKERS", "4"))) as pool:
        for done, (i, lpi, p) in enumerate(pool.imap_unordered(run_node, order), 1):
            lp[i] = lpi
            for k, a in p.items():
                pred[(k, i)] = a
            if done % 50 == 0:
                print(f"  {done}/{n} nodes, {time.perf_counter() - tic:.0f} s", flush=True)
    keys = sorted(pred)
    np.savez(OUT, lp=lp, pred_keys=np.array(keys), pred=np.array([pred[k] for k in keys]),
             **{k: v for k, v in G.items() if k not in ("paths", "nodes", "scored")},
             nodes=np.array(G["nodes"]), scored=np.array(G["scored"]), paths=np.array(G["paths"]))
    print(f"phase 1: {time.perf_counter() - tic:.0f} s", flush=True)


def weights(P, rho, chunk=2048):
    """P(ν is a cell's bin | data before window t) for every node and window."""
    n, T = P["lp"].shape
    res, parent, Y = P["res"], P["parent"], P["Y"].astype(float)
    # the region's evidence for its finest cells' counts (bayesbin's marginal includes the summed
    # count's own Poisson normaliser e^Y/Y!; the finest cells' allocation adds Y log(share)):
    # log Z~ = log marginal + Σ (log Y! − Y log area)
    inc = P["lp"] + gammaln(Y + 1) - Y * np.log(P["areas"])[:, None]
    Zt = np.concatenate([np.zeros((n, 1)), np.cumsum(inc, axis=1)[:, :-1]], axis=1)  # before window t
    beta_empty = 1.0 / (0.5 / WARMUP)  # an empty child: zero counts, the floored warm-up prior
    Zempty = np.log(beta_empty / (beta_empty + np.arange(T)))
    lr, l1r = np.log(rho), np.log1p(-rho)
    out = np.zeros((n, T))
    for a in range(0, T, chunk):
        b = min(a + chunk, T)
        Z = np.zeros((n, b - a))
        split = P["n_empty"][:, None] * Zempty[None, a:b]
        for r in range(RES, -1, -1):
            ii = np.nonzero(res == r)[0]
            if r == RES:
                Z[ii] = Zt[ii, a:b]
            else:
                Z[ii] = np.logaddexp(lr + Zt[ii, a:b], l1r + split[ii])
            if r > 0:
                np.add.at(split, parent[ii], Z[ii])
        pleaf = np.where((res == RES)[:, None], 1.0, np.exp(lr + Zt[:, a:b] - Z))
        reach = np.zeros((n, b - a))
        for r in range(0, RES + 1):
            ii = np.nonzero(res == r)[0]
            reach[ii] = 1.0 if r == 0 else reach[parent[ii]] * (1 - pleaf[parent[ii]])
        out[:, a:b] = reach * pleaf
    return out


def score(P, mix):
    """Per scored cell, the per-window (PIT, q_detect, log p) after the warm-up, for a predictive
    mix[k] = (log p, F(y-1), F(y)) arrays over windows."""
    out = []
    for k, (lg, lo, hi) in enumerate(mix):
        rng = np.random.default_rng(3000 + k)
        p = np.exp(lg)
        q = lo + rng.random(len(lo)) * p
        dq = np.array([conservative_q(a, b) for a, b in zip(lo, hi, strict=True)])
        out.append((q[WARMUP:], dq[WARMUP:], lg[WARMUP:]))
    return out


def report():
    P = dict(np.load(OUT, allow_pickle=False))
    scored = [str(c) for c in P["scored"]]
    paths, nrep = P["paths"], int(P["n_replay"])
    pred = {(int(k), int(i)): a for (k, i), a in zip(P["pred_keys"], P["pred"], strict=True)}
    T = P["lp"].shape[1]
    days = (T - WARMUP) * int(P["width"]) / 86400
    models = {}
    for r, name in [(3, "cell alone"), (2, "its resolution-2 cell"), (1, "its resolution-1 cell")]:
        models[name] = [tuple(pred[(k, int(paths[k][r]))][:, j] for j in range(3)) for k in range(len(scored))]
    W = {}
    for rho in RHOS:
        tic = time.perf_counter()
        W[rho] = weights(P, rho)
        tot = sum(W[rho][paths[:, r]] for r in range(RES + 1))  # each cell's bins: one per partition
        assert np.allclose(tot, 1.0), (rho, np.abs(tot - 1).max())
        mix = []
        for k in range(len(scored)):
            ws = [W[rho][int(paths[k][r])] for r in range(RES + 1)]
            ps = [pred[(k, int(paths[k][r]))] for r in range(RES + 1)]
            p = sum(w * np.exp(a[:, 0]) for w, a in zip(ws, ps, strict=True))
            lo = sum(w * a[:, 1] for w, a in zip(ws, ps, strict=True))
            hi = sum(w * a[:, 2] for w, a in zip(ws, ps, strict=True))
            mix.append((np.log(p), lo, hi))
        models[f"tree, ρ = {rho}"] = mix
        print(f"(tree weights and mixtures for ρ = {rho}: {time.perf_counter() - tic:.1f} s)")
    sc = {m: score(P, mix) for m, mix in models.items()}
    base = "cell alone"

    def table(ks, title):
        print(f"\n## {title}\n")
        print("| model | KS D | P(q>0.99) | P(q>0.999) | alarms/day per cell | log score vs cell alone, nats/day per cell |")
        print("|---|---|---|---|---|---|")
        for m in sc:
            q = np.concatenate([sc[m][k][0] for k in ks])
            dq = np.concatenate([sc[m][k][1] for k in ks])
            dl = sum(sc[m][k][2].sum() - sc[base][k][2].sum() for k in ks) / days / len(ks)
            print(f"| {m} | {kstest(q, 'uniform').statistic:.3f} | {np.mean(q > 0.99):.4f} | {np.mean(q > 0.999):.5f} "
                  f"| {np.sum(dq >= ALARM) / days / len(ks):.2f} | {dl:+.2f} |")

    table(range(nrep), f"The replay's {nrep} busiest cells (pooled)")
    table(range(nrep, len(scored)), f"{len(scored) - nrep} sparse cells (3-30 events in 3 months; pooled)")

    print(f"\n## Per cell: alarms per day and log score vs cell alone (nats per day), tree ρ = {RHOS[0]}\n")
    print("| cell | events | cell alone: alarms/day | tree: alarms/day | tree: log score vs cell alone | tree: weight on the cell itself (median) |")
    print("|---|---|---|---|---|---|")
    Y = P["Y"]
    tm = f"tree, ρ = {RHOS[0]}"
    for k in range(nrep):
        c = scored[k]
        w3 = W[RHOS[0]][int(paths[k][RES])][WARMUP:]
        print(f"| {c} | {Y[int(paths[k][RES])].sum()} | {np.sum(sc[base][k][1] >= ALARM) / days:.2f} "
              f"| {np.sum(sc[tm][k][1] >= ALARM) / days:.2f} | {(sc[tm][k][2].sum() - sc[base][k][2].sum()) / days:+.2f} "
              f"| {np.median(w3):.2f} |")

    k = scored.index(ALASKA)
    t0, w = int(P["t0"]), int(P["width"])
    ts = t0 + np.arange(T) * w
    onset = datetime(2026, 9, 1, tzinfo=UTC).timestamp()
    main_shock = datetime(2026, 9, 3, 11, 17, tzinfo=UTC).timestamp()
    print(f"\n## The Alaska sequence ({ALASKA}), tree ρ = {RHOS[0]}\n")
    print("Per 6 hours: events; P(the cell's bin is the cell itself / its res-2 / res-1 / res-0 ancestor), mean.\n")
    print("| 6 h from | events | res 3 | res 2 | res 1 | res 0 |")
    print("|---|---|---|---|---|---|")
    yk = Y[int(paths[k][RES])]
    for a in np.arange(onset - 2 * 86400, onset + 8 * 86400, 6 * 3600):
        sel = (ts >= a) & (ts < a + 6 * 3600)
        wr = [W[RHOS[0]][int(paths[k][r])][sel].mean() for r in (3, 2, 1, 0)]
        print(f"| {datetime.fromtimestamp(a, UTC):%m-%d %H:%M} | {yk[sel].sum()} | "
              + " | ".join(f"{x:.2f}" for x in wr) + " |")
    for m in (base, tm):
        dq = np.full(T, np.nan)
        dq[WARMUP:] = sc[m][k][1]
        first = np.flatnonzero((dq >= ALARM) & (ts >= onset))
        after = np.flatnonzero((dq >= ALARM) & (ts >= main_shock))

        def fmt(i: int) -> str:
            return datetime.fromtimestamp(ts[i], UTC).strftime("%m-%d %H:%M")

        print(f"\n{m}: first alarm after 09-01 00:00: {fmt(first[0]) if len(first) else 'none'}; after the M6.3: "
              f"first {fmt(after[0]) if len(after) else 'none'}, {len(after[ts[after] < main_shock + 3 * 86400])} alarm windows "
              "in the next 3 days")


if __name__ == "__main__":
    report() if sys.argv[1:] == ["report"] else phase1()
