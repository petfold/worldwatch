"""Summarise replay.py's results: calibration per stream, alarms, and the Alaska sequence
(cell 8322c4fffffffff, a swarm from 2026-09-01 and an M6.3 on 2026-09-03)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.stats import kstest

CACHE = Path.home() / ".cache" / "worldwatch-research"
WARMUP = 2 * 288
ALARM = 0.999
ALASKA = "8322c4fffffffff"


def when(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%m-%d %H:%M")


def main() -> None:
    d = np.load(CACHE / "usgs_streams.npz")
    r = dict(np.load(CACHE / "replay_results.npz"))
    mixture = (CACHE / "replay_results_mixture.npz").exists()
    if mixture:
        r.update(np.load(CACHE / "replay_results_mixture.npz"))
    models = [("cur", "current"), ("cp", "change-point, Poisson")] + ([("mx", "change-point, burst factor")] if mixture else [])
    t0, w = int(d["t0"]), int(d["width"])
    names = [str(n) for n in d["names"]]
    days = (len(d["counts"][0]) - WARMUP) * w / 86400
    print("## Calibration and alarms (after a 2-day warm-up)\n")
    print("| stream | events/window | model | KS D | P(q>0.99) | P(q>0.999) | P(q<0.01) | alarms/day |")
    print("|---|---|---|---|---|---|---|---|")
    for nm, counts in zip(names, d["counts"]):
        for model, label in models:
            q = r[f"{nm}|q_{model}"][WARMUP:]
            dq = r[f"{nm}|d_{model}"][WARMUP:]
            if model == "mx":
                label += f" (r = {np.median(r[f'{nm}|r_mx'][WARMUP:]):g})"
            print(f"| {nm if model == 'cur' else ''} | {counts.mean():.3f} | {label} | {kstest(q, 'uniform').statistic:.3f} "
                  f"| {np.mean(q > 0.99):.4f} | {np.mean(q > 0.999):.5f} | {np.mean(q < 0.01):.4f} "
                  f"| {np.sum(dq >= ALARM) / days:.2f} |")
    print(f"\nnominal: P(q>0.99) = 0.01, P(q>0.999) = 0.001, P(q<0.01) = 0.01; alarms need q_detect >= {ALARM}")

    j = names.index(ALASKA)
    counts = d["counts"][j]
    ts = t0 + np.arange(len(counts)) * w
    onset = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()
    main_shock = datetime(2026, 9, 3, 11, 17, tzinfo=timezone.utc).timestamp()
    print(f"\n## The Alaska sequence ({ALASKA})\n")
    print("Per 6 hours: events, and each model's alarms and expected events (its predictive mean summed).\n")
    extra = " burst factor: alarms | burst factor: expected |" if mixture else ""
    print("| 6 h from | events | current: alarms | current: expected | Poisson c-p: alarms | Poisson c-p: expected |"
          + extra + " averaging window (h) |")
    print("|---" * (7 + 2 * mixture) + "|")
    start = onset - 2 * 86400
    for a in np.arange(start, onset + 8 * 86400, 6 * 3600):
        sel = (ts >= a) & (ts < a + 6 * 3600)
        if not sel.any():
            continue
        print(f"| {when(a)} | {counts[sel].sum()} | {np.sum(r[f'{ALASKA}|d_cur'][sel] >= ALARM)} | "
              f"{np.nansum(r[f'{ALASKA}|m_cur'][sel]):.1f} | {np.sum(r[f'{ALASKA}|d_cp'][sel] >= ALARM)} | "
              f"{np.nansum(r[f'{ALASKA}|m_cp'][sel]):.1f} | "
              + (f"{np.sum(r[f'{ALASKA}|d_mx'][sel] >= ALARM)} | {np.nansum(r[f'{ALASKA}|m_mx'][sel]):.1f} | " if mixture else "")
              + f"{np.nanmean(r[f'{ALASKA}|run_cp'][sel]) * w / 3600:.1f} |")
    for model, label in models:
        dq = r[f"{ALASKA}|d_{model}"]
        first = np.flatnonzero((dq >= ALARM) & (ts >= onset))
        after = np.flatnonzero((dq >= ALARM) & (ts >= main_shock))
        print(f"\n{label}: first alarm after 09-01 00:00: {when(ts[first[0]]) if len(first) else 'none'}; "
              f"after the M6.3 (09-03 11:17): first {when(ts[after[0]]) if len(after) else 'none'}, "
              f"{len(after[ts[after] < main_shock + 3 * 86400])} alarm windows in the next 3 days")


if __name__ == "__main__":
    main()
