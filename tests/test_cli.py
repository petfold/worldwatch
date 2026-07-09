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


def test_cmd_detect_scores_and_returns_summary(db, sources):
    # one clean multi-bin group so Layer-0 writes surprise rows
    w = bin_width(3)
    base = (NOW // w) * w - 10 * w
    for i in range(5):
        db.execute(
            "INSERT INTO bins (stream_id, cell, scale, bin_start, n, vmin, vmax, vmean, m2) "
            "VALUES ('usgs_seismic','cellX',3,?,3,2.0,2.0,2.0,0.0)",
            (base + i * w,),
        )
    db.commit()

    result = cli.cmd_detect(db, sources)
    assert set(result) == {"surprise", "opened", "notified"}
    assert result["surprise"] == 5
    assert result["notified"] == 0  # no push channel configured in tests


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
