# Replay: the Layer-0 count model against a change-point model

Scripts behind [doc/research-changepoint-replay.md](../../doc/research-changepoint-replay.md).
Data are fetched into `~/.cache/worldwatch-research/` (not committed).

    PYTHONPATH=src .venv/bin/python research/replay_changepoint/fetch_usgs.py 2026-06-27 2026-09-27
    PYTHONPATH=src .venv/bin/python research/replay_changepoint/prepare.py        # needs h3
    PYTHONPATH=src <python with bayesbin>/python research/replay_changepoint/replay.py   # ~16 min
    <python with bayesbin>/python research/replay_changepoint/report.py

`replay.py` needs bayesbin (`pip install bayesbin`, 0.3.0); Worldwatch does not
depend on it.

`tree_replay.py` pools the cells over the H3 tree: a change-point stream per node
of the resolution-0 subtrees holding the replay's 12 cells (506 nodes), the tree's
weights after every window, and each cell's predictive as the mixture of its
ancestors' (the "Pooling over the H3 tree" section of the doc). It needs bayesbin
and h3 in one environment (bayesbin's `paper/twod/.venv` has both):

    <python with bayesbin and h3> research/replay_changepoint/tree_replay.py          # ~35 min on 4 cores
    <python with bayesbin and h3> research/replay_changepoint/tree_replay.py report   # seconds

`TREE_T=1200` runs the first 1,200 windows only, as a quick check; `TREE_WORKERS`
sets the number of processes (default 4).
