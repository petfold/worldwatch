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

    <python with bayesbin and h3> research/replay_changepoint/tree_replay.py          # ~90 min on 4 cores
    <python with bayesbin and h3> research/replay_changepoint/tree_replay.py report   # seconds

`TREE_T=1200` runs the first 1,200 windows only, as a quick check; `TREE_WORKERS`
sets the number of processes (default 4).

`tree_layer0.py` does the same with Layer 0's own model (`BayesianCount`,
vectorized over the nodes and checked against the class), over the whole tree,
scoring every cell with events. It needs only h3 and scipy (Worldwatch's
environment):

    .venv/bin/python research/replay_changepoint/tree_layer0.py check    # the replica against the class, ~2 min
    .venv/bin/python research/replay_changepoint/tree_layer0.py          # ~22 min on 4 cores
    .venv/bin/python research/replay_changepoint/tree_layer0.py report

`live_replay.py` runs a week of the catalogue through `LiveScorer` itself, pooled
and unpooled (a database each in the cache directory); `tree_inject.py` injects
swarms into the real counts and compares detection, Layer 0 today against the
pooled tree:

    PYTHONPATH=src .venv/bin/python research/replay_changepoint/live_replay.py pooled     # ~5 min
    PYTHONPATH=src .venv/bin/python research/replay_changepoint/live_replay.py unpooled   # ~20 min
    .venv/bin/python research/replay_changepoint/tree_inject.py                           # ~25 min

The EMSC catalogue (the events `emsc_seismic` ingests) for the same replays:

    PYTHONPATH=src .venv/bin/python research/replay_changepoint/fetch_emsc.py 2026-06-27 2026-09-27
    TREE_CATALOGUE=emsc .venv/bin/python research/replay_changepoint/tree_layer0.py          # ~55 min on 4 cores
    TREE_CATALOGUE=emsc .venv/bin/python research/replay_changepoint/tree_layer0.py report
    PYTHONPATH=src .venv/bin/python research/replay_changepoint/live_replay.py pooled emsc_seismic

GDELT's news counts (the `gdelt_events` stream) for the tree and the live path:

    PYTHONPATH=src .venv/bin/python research/replay_changepoint/fetch_gdelt.py 2026-09-03 2026-10-01   # ~20 min
    TREE_CATALOGUE=gdelt .venv/bin/python research/replay_changepoint/tree_layer0.py                   # ~9 min on 4 cores
    PYTHONPATH=src .venv/bin/python research/replay_changepoint/live_replay.py pooled gdelt_events 2026-09-20 2026-09-23

`live_replay.py KIND STREAM START END` replays another stream and span (emsc_*
streams read the EMSC files), with the stream's pooling switched on or off;
`live_compare.py STREAM` compares its pooled and unpooled databases over the
span both have scored (the tail, candidates, first reports, and candidates by
how active the cell's resolution-1 region was):

    PYTHONPATH=src .venv/bin/python research/replay_changepoint/live_replay.py pooled usgs_m45 2026-06-29 2026-09-27
    PYTHONPATH=src .venv/bin/python research/replay_changepoint/live_replay.py unpooled usgs_m45 2026-06-29 2026-09-27
    .venv/bin/python research/replay_changepoint/live_compare.py usgs_m45

