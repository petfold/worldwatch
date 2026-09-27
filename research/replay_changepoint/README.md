# Replay: the Layer-0 count model against a change-point model

Scripts behind [doc/research-changepoint-replay.md](../../doc/research-changepoint-replay.md).
Data are fetched into `~/.cache/worldwatch-research/` (not committed).

    PYTHONPATH=src .venv/bin/python research/replay_changepoint/fetch_usgs.py 2026-06-27 2026-09-27
    PYTHONPATH=src .venv/bin/python research/replay_changepoint/prepare.py        # needs h3
    PYTHONPATH=src <python with bayesbin>/python research/replay_changepoint/replay.py   # ~16 min
    <python with bayesbin>/python research/replay_changepoint/report.py

`replay.py` needs bayesbin (`pip install bayesbin`, 0.3.0); Worldwatch does not
depend on it.
