"""Pooling Layer 0's count model over the H3 tree, on the USGS replay (5-minute windows).

Worldwatch scores every (stream, cell) with its own BayesianCount (layer0/count.py). Here every
node of the H3 tree (each resolution-3 cell with events and every ancestor; the world itself is
taken to split, so the base cells' subtrees are independent) runs the same model on its region's
summed counts. A cell's predictive is the mixture of its own and its ancestors' predictives, each
at the cell's share of the ancestor's area, weighted by P(node is the cell's bin | data so far)
from the tree recursion

    Z(ν) = ρ S(ν) + (1 − ρ) Π_children Z(χ)

on the nodes' log predictive scores S, summed with a forgetting time (dynamic model averaging).
Every cell with events is scored, every window after the 2-day warm-up, as replay.py scores:
randomized PIT, the conservative q of ADR 0003 (alarms: q >= 0.999), and the log predictive.

The model is BayesianCount vectorized over the nodes, with the usgs_seismic stanza's settings
(Poisson and the burst grid, prior Gamma(0.5, 1e-3), 3-day memory, no seasonality). Its gamma
quantiles come from a table interpolated in log shape (relative error about 1e-6); `check` runs
the class itself beside it.

    <python with h3 and scipy> research/replay_changepoint/tree_layer0.py check    # the replica
    <python with h3 and scipy> research/replay_changepoint/tree_layer0.py          # ~15 min, 4 cores

TREE_CATALOGUE=emsc runs the same on the EMSC catalogue (fetch_emsc.py; the emsc_seismic stream),
over the USGS replay's windows, into tree_layer0_emsc.npz.
"""

from __future__ import annotations

import os

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")  # one BLAS thread per worker process

import csv
import glob
import math
import sys
import time
from datetime import UTC, datetime
from multiprocessing import Pool
from pathlib import Path

import h3
import numpy as np
from scipy.special import betainc, gammaincinv, gammaln

CACHE = Path.home() / ".cache" / "worldwatch-research"
SRC = Path(__file__).resolve().parents[2] / "src"
WARMUP = 2 * 288
ALARM = 0.999
RES = 3
RHO = 0.1
MEMORIES = {"3 days": 3 * 86400, "30 days": 30 * 86400, "all": math.inf}  # of the pooling evidence
ALASKA = "8322c4fffffffff"
CATALOGUE = os.environ.get("TREE_CATALOGUE", "usgs")  # or "emsc" (fetch_emsc.py)
RESULTS = CACHE / ("tree_layer0.npz" if CATALOGUE == "usgs" else f"tree_layer0_{CATALOGUE}.npz")

# BayesianCount's settings for usgs_seismic (layer0/count.py defaults)
K_FIN = np.array([0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0])
A0, B0 = 0.5, 1e-3
MEMORY = 3 * 86400
N_Q = 24
U = (np.arange(N_Q) + 0.5) / N_Q
EPS = 1e-12

# the standard gamma's quantiles at U, tabulated in log shape (shape >= A0 always)
_LA = np.linspace(np.log(A0), np.log(1e6), 6001)
_LQ = np.log(gammaincinv(np.exp(_LA)[:, None], U[None, :]))
_DLA = _LA[1] - _LA[0]


def quantiles(a: np.ndarray) -> np.ndarray:
    """gammaincinv(a, U) for an array of shapes a (any shape), by interpolation: a[..., None, :]."""
    x = (np.log(a) - _LA[0]) / _DLA
    i = np.clip(x.astype(int), 0, len(_LA) - 2)
    f = (x - i)[..., None]
    return np.exp((1 - f) * _LQ[i] + f * _LQ[i + 1])


class Batch:
    """BayesianCount for many series at once (exposure 1 per window; Poisson and the burst grid)."""

    def __init__(self, n: int):
        self.n = n
        self.a = self.b = None
        self.logw = np.full((n, len(K_FIN) + 1), -math.log(len(K_FIN) + 1))
        self.delta = math.exp(-300 / MEMORY)

    def start(self, y: np.ndarray) -> None:
        self.a = np.full((self.n, len(K_FIN) + 1), A0) + y[:, None]
        self.b = np.full((self.n, len(K_FIN) + 1), B0 + 1.0)

    def discounted(self):
        a = self.delta * self.a + (1 - self.delta) * A0
        b = self.delta * self.b + (1 - self.delta) * B0
        return a, b

    @staticmethod
    def predictive(y, e, a, b, lam):
        """Per hypothesis P(Y = y) and P(Y < y) at exposure e, for rows y (n,), e (n,), a, b (n, 10),
        lam (n, 9, N_Q) the finite-k quadrature nodes of λ ~ Gamma(a, b)."""
        n = len(y)
        pmf = np.empty((n, len(K_FIN) + 1))
        below = np.zeros((n, len(K_FIN) + 1))
        k = K_FIN[None, :, None]
        p = k / (k + np.maximum(lam * e[:, None, None], EPS))  # (n, 9, N_Q)
        pp = b[:, -1] / (b[:, -1] + e)
        zero = y == 0
        pmf[zero, :-1] = np.exp(k[0] * np.log(p[zero])).mean(axis=2)
        pmf[zero, -1] = np.exp(a[zero, -1] * np.log(pp[zero]))
        nz = ~zero
        if nz.any():
            yy = y[nz][:, None, None].astype(float)
            pz = p[nz]
            lp = gammaln(yy + k) - gammaln(k) - gammaln(yy + 1) + k * np.log(pz) + yy * np.log1p(-pz)
            pmf[nz, :-1] = np.exp(lp).mean(axis=2)
            below[nz, :-1] = betainc(k, yy, pz).mean(axis=2)
            y1, a1, q1 = y[nz].astype(float), a[nz, -1], pp[nz]
            pmf[nz, -1] = np.exp(gammaln(y1 + a1) - gammaln(a1) - gammaln(y1 + 1) + a1 * np.log(q1) + y1 * np.log1p(-q1))
            below[nz, -1] = betainc(a1, y1, q1)
        return pmf, below

    def lam(self, a, b):
        return quantiles(a[:, :-1]) / b[:, :-1, None]

    def update(self, y: np.ndarray, a, b, pmf) -> None:
        self.logw = self.delta * self.logw + np.log(np.clip(pmf, EPS, None))
        self.logw -= np.logaddexp.reduce(self.logw, axis=1)[:, None]
        lam_hat = a / b
        k = np.append(K_FIN, np.inf)[None, :]
        with np.errstate(invalid="ignore", divide="ignore"):
            e_omega = np.where(np.isinf(k), 1.0, (k + y[:, None]) / (k + lam_hat))
        self.a = a + y[:, None]
        self.b = b + e_omega

    @property
    def w(self):
        return np.exp(self.logw - np.logaddexp.reduce(self.logw, axis=1)[:, None])


def conservative(lo, hi):
    return np.where(lo > 0.5, lo, np.where(hi < 0.5, hi, 0.5))


def area(c: str) -> float:
    if h3.get_resolution(c) == RES:
        return h3.cell_area(c, "km^2")
    return sum(h3.cell_area(d, "km^2") for d in h3.cell_to_children(c, RES))


def build():
    d = np.load(CACHE / "usgs_streams.npz")
    t0, width, T = int(d["t0"]), int(d["width"]), d["counts"].shape[1]
    rows, big = [], []
    for f in sorted(glob.glob(str(CACHE / CATALOGUE / "*.csv"))):
        with open(f) as fh:
            for r in csv.DictReader(fh):
                ts = datetime.fromisoformat(r["time"].replace("Z", "+00:00")).timestamp()
                rows.append((int((ts - t0) // width), h3.latlng_to_cell(float(r["latitude"]), float(r["longitude"]), RES)))
                if float(r["mag"] or 0.0) >= 5.0 and 0 <= rows[-1][0] < T:
                    big.append((rows[-1][0], rows[-1][1], float(r["mag"])))
    cells = sorted({c for _, c in rows})
    nodes = sorted(set(cells) | {h3.cell_to_parent(c, r) for c in cells for r in range(RES)},
                   key=lambda c: (h3.get_resolution(c), c))
    index = {c: i for i, c in enumerate(nodes)}
    Y = np.zeros((len(nodes), T), np.int16)
    for w, c in rows:
        if 0 <= w < T:
            for r in range(RES + 1):
                Y[index[h3.cell_to_parent(c, r) if r < RES else c], w] += 1
    replay = [str(n) for n in d["names"]][1:] if CATALOGUE == "usgs" else []
    for j, c in enumerate(replay):
        assert np.array_equal(Y[index[c]], d["counts"][j + 1]), c
    res = np.array([h3.get_resolution(c) for c in nodes])
    parent = np.array([index[h3.cell_to_parent(c, r - 1)] if r > 0 else -1 for c, r in zip(nodes, res, strict=True)])
    areas = np.array([area(c) for c in nodes])
    n_empty = np.array([0 if r == RES else sum(k not in index for k in h3.cell_to_children(c, int(r) + 1))
                        for c, r in zip(nodes, res, strict=True)])
    root = np.array([index[h3.cell_to_parent(c, 0)] for c in nodes])
    if CATALOGUE == "usgs":  # (the same quakes as above, in prepare.py's order)
        big = [(int((ts - t0) // width), str(c), float(m)) for ts, c, m in zip(d["big_ts"], d["big_cell"], d["big_mag"], strict=True)]
    return dict(t0=t0, width=width, T=T, nodes=nodes, Y=Y, res=res, parent=parent, areas=areas,
                n_empty=n_empty, root=root, cells=cells, replay=replay, big=big)


VARIANTS = ["cell alone (Layer 0 today)", "the resolution-2 cell", *[f"tree, memory {m}" for m in MEMORIES]]
G = None


def run_group(roots):
    """The nodes under the given base cells: their models, the tree's weights and every cell's
    scores, window by window."""
    sel = np.flatnonzero(np.isin(G["root"], roots))
    local = {g: i for i, g in enumerate(sel)}
    n = len(sel)
    Y = G["Y"][sel].astype(np.int64)
    res, areas, n_empty = G["res"][sel], G["areas"][sel], G["n_empty"][sel]
    parent = np.array([local[p] if p >= 0 else -1 for p in G["parent"][sel]])
    cells = [i for i in range(n) if res[i] == RES]
    path = np.array([[i if r == RES else None for r in range(RES + 1)] for i in cells], dtype=object)
    for row, i in zip(path, cells, strict=True):  # each cell's ancestors at resolutions 0..3
        j = i
        for r in range(RES, -1, -1):
            row[r] = j
            j = parent[j]
    path = path.astype(int)
    share = areas[path[:, RES]][:, None] / areas[path]  # (C, 4)
    C = len(cells)
    yc = Y[path[:, RES]]  # (C, T)
    m = Batch(n + 1)  # the last series: an empty region (zero counts throughout)
    m.start(np.append(Y[:, 0], 0))
    corr = gammaln(Y + 1.0) - Y * np.log(areas)[:, None]  # the finest cells' allocation
    S = {k: np.zeros(n + 1) for k in MEMORIES}
    dpool = {k: (0.0 if math.isinf(v) else math.exp(-300 / v)) for k, v in MEMORIES.items()}
    lr, l1r = math.log(RHO), math.log1p(-RHO)
    levels = [np.flatnonzero(res == r) for r in range(RES + 1)]
    fixed = [np.tile(np.eye(RES + 1)[RES], (C, 1)), np.tile(np.eye(RES + 1)[2], (C, 1))]
    rng = np.random.default_rng(int(sel[0]))
    nv = len(VARIANTS)
    acc = {"alarms": np.zeros((nv, C)), "logp": np.zeros((nv, C)), "q99": np.zeros((nv, C)),
           "q999": np.zeros((nv, C)), "q01": np.zeros((nv, C)), "hist": np.zeros((nv, C, 200))}
    alaska = G["nodes"].index(ALASKA) if ALASKA in G["nodes"] else -1
    keep = {}  # per-window q_detect of the Alaska cell, and of every big quake's cell
    watch = [k for k, i in enumerate(cells) if sel[i] == alaska or any(G["nodes"][sel[i]] == c for _, c, _ in G["big"])]
    for k in watch:
        keep[G["nodes"][sel[cells[k]]]] = np.full((nv, G["T"]), np.nan)
    wpath_log = np.zeros((C, RES + 1))
    for t in range(1, G["T"]):
        a, b = m.discounted()
        lam = m.lam(a, b)
        w = m.w
        ynode = np.append(Y[:, t], 0)
        pmf, _ = Batch.predictive(ynode, np.ones(n + 1), a, b, lam)
        p_node = (w * pmf).sum(axis=1)
        inc = np.log(np.clip(p_node, 1e-300, None))
        inc[:n] += corr[:, t]
        # the tree's weights for each forgetting time
        pbin = {}
        for key in MEMORIES:
            S[key] = (S[key] * dpool[key] + inc) if dpool[key] > 0 else S[key] + inc
            Sn, Se = S[key][:n], S[key][n]
            Z = np.zeros(n)
            split = n_empty * Se
            for r in range(RES, -1, -1):
                ii = levels[r]
                Z[ii] = Sn[ii] if r == RES else np.logaddexp(lr + Sn[ii], l1r + split[ii])
                if r > 0:
                    np.add.at(split, parent[ii], Z[ii])
            pleaf = np.where(res == RES, 1.0, np.exp(np.clip(lr + Sn - Z, -700, 0)))
            reach = np.zeros(n)
            for r in range(RES + 1):
                ii = levels[r]
                reach[ii] = 1.0 if r == 0 else reach[parent[ii]] * (1 - pleaf[parent[ii]])
            pbin[key] = reach * pleaf
        # each cell's predictive under each of its four candidates
        flat = path.ravel()
        yy = np.repeat(yc[:, t], RES + 1)
        pm, bl = Batch.predictive(yy, share.ravel(), a[flat], b[flat], lam[flat])
        p = (w[flat] * pm).sum(axis=1).reshape(C, RES + 1)
        lo = (w[flat] * bl).sum(axis=1).reshape(C, RES + 1)
        if t >= WARMUP:
            weights = fixed + [pbin[key][path] for key in MEMORIES]
            if t == WARMUP:
                for wv in weights[2:]:  # a cell's bins, one per partition: the weights sum to one
                    assert np.allclose(wv.sum(axis=1), 1.0), np.abs(wv.sum(axis=1) - 1).max()
            u = rng.random(C)
            for v, wv in enumerate(weights):
                pv = (wv * p).sum(axis=1)
                lv = (wv * lo).sum(axis=1)
                q = np.clip(lv + u * pv, 0, 1)
                dq = conservative(lv, np.minimum(1.0, lv + pv))
                acc["alarms"][v] += dq >= ALARM
                acc["logp"][v] += np.log(np.clip(pv, 1e-300, None))
                acc["q99"][v] += q > 0.99
                acc["q999"][v] += q > 0.999
                acc["q01"][v] += q < 0.01
                acc["hist"][v, np.arange(C), np.minimum((q * 200).astype(int), 199)] += 1
                for k in watch:
                    keep[G["nodes"][sel[cells[k]]]][v, t] = dq[k]
            wpath_log += pbin["30 days"][path]
        m.update(ynode, a, b, pmf)
    names = [G["nodes"][sel[i]] for i in cells]
    events = Y[path[:, RES]].sum(axis=1)
    return names, events, acc, keep, wpath_log / (G["T"] - WARMUP)


def check():
    """The replica against BayesianCount itself, on the replay's 12 cells: alarms and predictives."""
    sys.path.insert(0, str(SRC))
    from worldwatch.layer0.count import BayesianCount

    d = np.load(CACHE / "usgs_streams.npz")
    T = 3000
    counts = d["counts"][1:, :T].astype(np.int64)
    m = Batch(len(counts))
    m.start(counts[:, 0])
    ref = [BayesianCount(seed=1) for _ in counts]
    for r, y in zip(ref, counts[:, 0], strict=True):
        r.update(int(d["t0"]), float(y))
    worst_p = worst_lo = 0.0
    flips = 0
    for t in range(1, T):
        a, b = m.discounted()
        pmf, below = Batch.predictive(counts[:, t], np.ones(len(counts)), a, b, m.lam(a, b))
        w = m.w
        p, lo = (w * pmf).sum(axis=1), (w * below).sum(axis=1)
        for j, r in enumerate(ref):
            aa = r._a * math.exp(-300 / r.memory_seconds) + (1 - math.exp(-300 / r.memory_seconds)) * r.prior_shape
            bb = r._b * math.exp(-300 / r.memory_seconds) + (1 - math.exp(-300 / r.memory_seconds)) * r.prior_rate
            pm_ref, bl_ref = r._predictive(int(counts[j, t]), 1.0, aa, bb)
            p_ref, lo_ref = float(np.sum(r.weights * pm_ref)), float(np.sum(r.weights * bl_ref))
            worst_p = max(worst_p, abs(p[j] - p_ref) / max(p_ref, 1e-300))
            worst_lo = max(worst_lo, abs(lo[j] - lo_ref))
            r.update(int(d["t0"]) + 300 * t, float(counts[j, t]))
            flips += (conservative(lo[j], min(1, lo[j] + p[j])) >= ALARM) != (r.last_detect_q >= ALARM)
        m.update(counts[:, t], a, b, pmf)
    print(f"{T} windows x 12 cells: worst relative error in P(y) {worst_p:.1e}, in P(Y < y) {worst_lo:.1e}; "
          f"alarm decisions that differ: {flips}")


def main():
    global G
    G = build()
    roots = sorted({int(r) for r in G["root"]})
    sizes = {r: int((G["root"] == r).sum()) for r in roots}
    groups = [[] for _ in range(int(os.environ.get("TREE_WORKERS", "4")) * 3)]
    load = [0] * len(groups)
    for r in sorted(roots, key=lambda r: -sizes[r]):  # balance the groups by nodes
        g = int(np.argmin(load))
        groups[g].append(r)
        load[g] += sizes[r]
    print(f"{len(G['nodes'])} nodes, {len(G['cells'])} cells, {G['T']} windows, {len(groups)} groups", flush=True)
    tic = time.perf_counter()
    out = []
    with Pool(int(os.environ.get("TREE_WORKERS", "4"))) as pool:
        for k, r in enumerate(pool.imap_unordered(run_group, [g for g in groups if g]), 1):
            out.append(r)
            print(f"  group {k} done, {time.perf_counter() - tic:.0f} s", flush=True)
    names = sum((o[0] for o in out), [])
    events = np.concatenate([o[1] for o in out])
    acc = {k: np.concatenate([o[2][k] for o in out], axis=1) for k in out[0][2]}
    keep = {}
    for o in out:
        keep.update(o[3])
    wbar = np.concatenate([o[4] for o in out])
    np.savez(RESULTS, names=np.array(names), events=events, wbar=wbar,
             keep_names=np.array(list(keep)), keep=np.array(list(keep.values())), variants=np.array(VARIANTS),
             big=np.array([(w, c, mg) for w, c, mg in G["big"]], dtype=object), t0=G["t0"], width=G["width"],
             T=G["T"], replay=np.array(G["replay"]), **acc)
    print(f"done: {time.perf_counter() - tic:.0f} s", flush=True)


def report():
    R = np.load(RESULTS, allow_pickle=True)
    names, events, variants = [str(x) for x in R["names"]], R["events"], [str(v) for v in R["variants"]]
    T, width, t0 = int(R["T"]), int(R["width"]), int(R["t0"])
    days = (T - WARMUP) * width / 86400
    n = T - WARMUP
    classes = [("busy (300+ events)", events >= 300), ("31-299 events", (events >= 31) & (events < 300)),
               ("3-30 events", (events >= 3) & (events <= 30)), ("1-2 events", events <= 2)]
    print(f"{len(names)} cells, {days:.0f} days scored; log score per cell and day against Layer 0 today\n")
    for label, sel in classes:
        print(f"### {label}: {sel.sum()} cells\n")
        print("| model | KS D | P(q>0.99) | P(q>0.999) | P(q<0.01) | alarms/day per cell | log score |")
        print("|---|---|---|---|---|---|---|")
        for v, name in enumerate(variants):
            h = R["hist"][v][sel].sum(axis=0)
            cdf = np.cumsum(h) / h.sum()
            ks = np.max(np.abs(cdf - np.arange(1, 201) / 200))
            tot = sel.sum() * n
            dl = (R["logp"][v][sel] - R["logp"][0][sel]).sum() / days / sel.sum()
            print(f"| {name} | {ks:.3f} | {R['q99'][v][sel].sum() / tot:.4f} | {R['q999'][v][sel].sum() / tot:.5f} "
                  f"| {R['q01'][v][sel].sum() / tot:.4f} | {R['alarms'][v][sel].sum() / days / sel.sum():.3f} "
                  f"| {dl:+.3f} |")
        print()
    rep = [str(c) for c in R["replay"]]
    idx = [names.index(c) for c in rep]
    if rep:
        print("### The replay's 12 cells: alarms per day (Layer 0 today should equal replay.py's current model)\n")
        print("| cell | " + " | ".join(variants) + " |")
        print("|---|" + "---|" * len(variants))
    for c, i in zip(rep, idx, strict=True):
        print(f"| {c} | " + " | ".join(f"{R['alarms'][v][i] / days:.2f}" for v in range(len(variants))) + " |")
    keep = dict(zip([str(x) for x in R["keep_names"]], R["keep"], strict=True))
    big = [(int(w), str(c), float(mg)) for w, c, mg in R["big"] if int(w) >= WARMUP]
    print(f"\n### The {len(big)} quakes of M >= 5 after the warm-up: an alarm in their cell within an hour\n")
    print("| model | in the quake's window | within an hour |")
    print("|---|---|---|")
    for v, name in enumerate(variants):
        at = sum(keep[c][v, w] >= ALARM for w, c, _ in big if c in keep)
        hour = sum(np.nanmax(keep[c][v, w:w + 12]) >= ALARM for w, c, _ in big if c in keep)
        print(f"| {name} | {at} of {len(big)} | {hour} of {len(big)} |")
    ts = t0 + np.arange(T) * width
    onset = datetime(2026, 9, 1, tzinfo=UTC).timestamp()
    main_shock = datetime(2026, 9, 3, 11, 17, tzinfo=UTC).timestamp()
    print(f"\n### The Alaska sequence ({ALASKA})\n")
    for v, name in enumerate(variants if ALASKA in keep else []):
        dq = keep[ALASKA][v]
        first = np.flatnonzero((dq >= ALARM) & (ts >= onset))
        after = np.flatnonzero((dq >= ALARM) & (ts >= main_shock))

        def fmt(i: int) -> str:
            return datetime.fromtimestamp(ts[i], UTC).strftime("%m-%d %H:%M")

        print(f"- {name}: first alarm after 09-01 00:00 {fmt(first[0]) if len(first) else 'none'}; after the M6.3 "
              f"{fmt(after[0]) if len(after) else 'none'}; {len(after[ts[after] < main_shock + 3 * 86400])} alarm "
              "windows in the next 3 days")
    w = R["wbar"]
    print("\n### Mean weight by level (tree, memory 30 days)\n")
    print("| class | res 3 (the cell) | res 2 | res 1 | res 0 |")
    print("|---|---|---|---|---|")
    for label, sel in classes:
        print(f"| {label} | " + " | ".join(f"{w[sel][:, r].mean():.2f}" for r in (3, 2, 1, 0)) + " |")


if __name__ == "__main__":
    {"check": check, "report": report}.get(sys.argv[1] if len(sys.argv) > 1 else "", main)()
