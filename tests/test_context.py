"""Plain-language context for pushes and the dashboard (presentation only)."""

import math

import h3

from worldwatch.api import context


def _bin(n=1, vmin=None, vmax=None, vmean=None, scale=2, start=1_000_000):
    return {"n": n, "vmin": vmin, "vmax": vmax, "vmean": vmean, "scale": scale, "bin_start": start}


def test_display_from_stanza(sources):
    d = context.display_for("btc_usd", sources["btc_usd"])
    assert d.label == "Bitcoin" and d.prefix == "$" and d.inverse == "expm1"
    assert context.display_for("unknown", None).label == "unknown"


def test_describe_count_bin_with_max(sources):
    text = context.describe_bin("usgs_seismic", sources["usgs_seismic"], _bin(n=4, vmax=5.14))
    assert text == "4 quakes, max M5.1"
    one = context.describe_bin("usgs_seismic", sources["usgs_seismic"], _bin(n=1, vmax=7.0))
    assert one == "1 quake, max M7.0"


def test_describe_continuous_undoes_log1p(sources):
    text = context.describe_bin("btc_usd", sources["btc_usd"], _bin(vmean=math.log1p(84_000)))
    assert text == "$84,000"


def test_describe_percent(sources):
    cfg = sources["cf_radar_netflows_gb"]
    assert context.describe_bin(cfg.stream_id, cfg, _bin(vmean=0.771)) == "77% of 7-day peak"


def test_rarity_two_sided_and_capped():
    assert context.rarity(0.999) == ("1-in-1,000", "high")
    assert context.rarity(0.004) == ("1-in-250", "low")
    assert context.rarity(1e-15)[0] == "over 1-in-1M"


def test_surprise_word():
    assert context.surprise_word(0.6) == "typical"
    assert context.surprise_word(0.95) == "unusual, 1-in-20 high"
    assert context.surprise_word(0.001) == "rare, 1-in-1,000 low"


def test_where_and_map_url():
    cell = h3.latlng_to_cell(35.68, 139.69, 3)
    assert context.where(cell).endswith("E") and "N" in context.where(cell)
    assert context.where("GLOBAL") == "GLOBAL"
    assert context.map_url("GLOBAL") is None
    assert "openstreetmap.org" in context.map_url(cell)


def test_bin_row_falls_back_to_latest_earlier_bin(db):
    db.execute(
        "INSERT INTO bins (stream_id, cell, scale, bin_start, n, vmin, vmax, vmean) "
        "VALUES ('s', 'c', 3, 1000, 2, 1, 2, 1.5)"
    )
    assert context.bin_row(db, "s", "c", 3, 1000)["n"] == 2
    # the scored bin folded away (other scale): nearest earlier bin is used
    assert context.bin_row(db, "s", "c", 2, 1200)["bin_start"] == 1000
