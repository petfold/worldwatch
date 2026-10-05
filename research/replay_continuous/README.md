# Replaying the continuous model on real histories

The evidence for ADR 0007. `fetch.py` pulls public histories into `data/`
(kept out of git): Coinbase 1-minute BTC and ETH closes, PEGELONLINE river
levels (30 gauges, hourly), Elexon GB frequency, GOES X-ray flux. The 12
Cloudflare Radar series need the API token, so they were fetched on the VPS
(28 days at 15 min) and converted to `data/cf.csv`.

`replay.py` runs the production model (`run_v3`: `make_model` from the stanza,
plus any overrides) over each cell's series, skips 2 days of warm-up, and judges
the PITs as the nursery does (`judge`: TV of the decile histogram, 1% tail
ratios). `PROPOSED` holds the stanza changes ADR 0007 adopted. The `Relative`
and `Quantized` classes are the experiments that led there.

    cd research/replay_continuous
    ../../.venv/bin/python fetch.py
    ../../.venv/bin/python -c "from replay import *; show('btc', 'v3', run_v3('btc', **PROPOSED['btc']))"
