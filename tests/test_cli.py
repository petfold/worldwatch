"""CLI process entrypoints: init, consolidate, detect, presence, main dispatch."""

import json

from worldwatch import cli
from worldwatch.cascade.bins import bin_width
from worldwatch.ingest.models import Observation
from worldwatch.store import write_observations

NOW = 2_000_000_000


def test_cmd_init_registers_sources(db, sources):
    n = cli.cmd_init(db, sources)
    assert n == len(sources)
    rows = db.execute("SELECT stream_id, modality, flavor FROM sources").fetchall()
    assert len(rows) == len(sources)
    # idempotent
    cli.cmd_init(db, sources)
    assert db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == len(sources)


def test_cmd_consolidate(db, sources):
    # cmd_consolidate uses real wall-clock now; use timestamps safely in the past
    old = 1_600_000_000
    write_observations(db, [Observation("btc_usd", "GLOBAL", old + i, 100.0) for i in range(10)])
    n = cli.cmd_consolidate(db, fine_window_seconds=0)
    assert n == 10
    assert db.execute("SELECT COUNT(*) FROM bins").fetchone()[0] >= 1


def test_cmd_detect_sweeps_alerts_and_returns_summary(db, sources):
    """detect is the safety-net sweep: scoring happens live in poll (ADR 0002);
    detect applies the alert policy to the archive and pushes what it opens."""
    import time

    import h3

    now = int(time.time())
    uk = h3.latlng_to_cell(54.0, -2.5, 2)  # cf_radar_netflows_gb's cell
    quake = h3.cell_to_children(uk, 3)[0]
    # more quakes, less traffic (a traffic surge is no evidence: its stanza's tail is "lower")
    for sid, cell, q in (("usgs_m45", quake, 0.9999), ("cf_radar_netflows_gb", uk, 0.0001)):
        db.execute(
            "INSERT INTO surprise (stream_id, cell, scale, bin_start, q_value, presence_q, "
            "precision, n_obs, tail_index, model_version) "
            "VALUES (?, ?, -1, ?, ?, 1, 1, 3, NULL, 2)",
            (sid, cell, now - 60, q),
        )
    db.commit()
    # physical + infrastructural in one region → one alert; no push channel configured
    assert cli.cmd_detect(db, sources) == {"opened": 1, "notified": 0}
    assert cli.cmd_detect(db, sources) == {"opened": 0, "notified": 0}  # idempotent


def test_cmd_presence(db, sources):
    # no health rows → nothing to score
    assert cli.cmd_presence(db, sources) == 0


def test_main_dispatch_uses_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WW_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.delenv("WW_CONFIG_DIR", raising=False)  # use packaged stanzas
    monkeypatch.delenv("WW_NTFY_TOPIC", raising=False)

    assert cli.main(["init"]) == 0
    out = capsys.readouterr().out
    assert "registered" in out

    # detect runs cleanly against the freshly-initialized DB
    assert cli.main(["detect"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["opened"] == 0
