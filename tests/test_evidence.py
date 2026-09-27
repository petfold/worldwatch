"""Evidence store: slim human-readable records beside the detection path."""

import json
import time

import h3
import httpx

from conftest import FIXTURES, load_fixture
from worldwatch import evidence
from worldwatch.api.notify import NtfyConfig, format_alert, send_ntfy
from worldwatch.cascade.consolidator import consolidate
from worldwatch.ingest import parsers
from worldwatch.ingest.models import Observation
from worldwatch.ingest.parsers import headline_from_url
from worldwatch.store import write_observations

CELL = h3.latlng_to_cell(-21.3, 168.6, 3)


def _quake(ts, mag, place="80 km ENE of Tadine, New Caledonia", url=None):
    ctx = {"place": place, "mag": mag, "depth_km": 10.0, "url": url or f"https://earthquake.usgs.gov/e/{ts}"}
    return Observation("usgs_seismic", CELL, ts, mag, context={**ctx, "_rank": mag})


# --- parsers keep what the stanza names ----------------------------------------


def test_usgs_parser_keeps_place_and_rank(sources):
    obs = parsers.parse(load_fixture("usgs_sample.json"), sources["usgs_seismic"])
    ctx = obs[0].context
    assert ctx["place"] and "mag" in ctx and "_rank" in ctx
    assert "time" not in ctx and "type" not in ctx  # only the named fields


def test_gdelt_parser_keeps_action_place_link(sources):
    content = (FIXTURES / "gdelt_export_sample.zip").read_bytes()
    obs = parsers.parse({"content": content, "batch_epoch": 1783803600}, sources["gdelt_events"])
    ctx = obs[0].context
    assert ctx["action"] in parsers.CAMEO_ROOTS.values()
    assert str(ctx["url"]).startswith("http") and ctx["place"]
    assert isinstance(ctx.get("mentions"), int)


def test_stanza_without_context_keeps_none(sources):
    obs = parsers.parse(load_fixture("cloudflare_radar_sample.json"), sources["cf_radar_netflows_gb"])
    assert all(o.context is None for o in obs)


def test_headline_from_url():
    u = "https://www.mirror.co.uk/news/uk-news/jay-slaters-mum-accidentally-glanced-37704758"
    assert headline_from_url(u) == "Jay slaters mum accidentally glanced"
    assert headline_from_url("https://x.org/26555931.south-wales-police-told") == "South wales police told"
    assert headline_from_url("https://x.org/article/12345") == ""


# --- storage, FIFO budget, retrieval ---------------------------------------------


def test_context_written_once_alongside_new_observations(db):
    assert write_observations(db, [_quake(1000, 6.6)]) == 1
    assert write_observations(db, [_quake(1000, 6.6)]) == 0  # re-fetch
    (row,) = db.execute("SELECT rank, data FROM context").fetchall()
    assert row["rank"] == 6.6 and "_rank" not in json.loads(row["data"])


def test_prune_evicts_oldest_first_to_budget(db):
    write_observations(db, [_quake(1000 + i, 1.0 + i / 10) for i in range(20)])
    sizes = [r[0] for r in db.execute("SELECT size FROM context ORDER BY rowid")]
    budget = sum(sizes[-5:])
    evidence.prune(db, budget)
    kept = [r[0] for r in db.execute("SELECT ts FROM context ORDER BY ts")]
    assert kept == [1015, 1016, 1017, 1018, 1019]
    assert db.execute("SELECT SUM(size) FROM context").fetchone()[0] <= budget


def test_consolidator_applies_the_budget(db):
    write_observations(db, [_quake(1000 + i, 2.0) for i in range(10)], now=2000)
    consolidate(db, now=5000, fine_window_seconds=900, context_budget_bytes=0)
    assert db.execute("SELECT COUNT(*) FROM context").fetchone()[0] == 0
    assert db.execute("SELECT SUM(n) FROM bins").fetchone()[0] == 10  # detection path untouched


def test_top_ranks_and_dedups_by_link(db):
    write_observations(db, [_quake(1000, 4.1), _quake(1001, 6.6), _quake(1002, 5.0, url="https://dup"),
                            _quake(1003, 5.2, url="https://dup")])
    recs = evidence.top(db, "usgs_seismic", CELL, 900, 1100, limit=3)
    assert [r["mag"] for r in recs] == [6.6, 5.2, 4.1]


def test_summary_tolerates_missing_fields(sources):
    cfg = sources["usgs_seismic"]
    assert evidence.summary(cfg, {"mag": 6.6, "place": "Tadine", "depth_km": 10.4}) == "M6.6 Tadine, depth 10 km"
    assert evidence.summary(cfg, {"mag": 6.6}) == "M6.6"


# --- the 3 a.m. push says what happened ------------------------------------------


def _alert_row(db):
    ev = [{"stream_id": "usgs_seismic", "modality": "physical", "q_value": 0.9996, "presence_q": 1.0,
           "cell": CELL, "scale": 0, "bin_start": 900}]
    db.execute(
        "INSERT INTO alerts (alert_id, opened_at, status, severity, cell, scale, evidence) "
        "VALUES (7, 1000, 'open', 0.95, ?, 0, ?)",
        (h3.cell_to_parent(CELL, 2), json.dumps(ev)),
    )
    db.commit()
    return db.execute("SELECT * FROM alerts WHERE alert_id = 7").fetchone()


def test_push_text_carries_the_story(db, sources):
    write_observations(db, [_quake(1000, 6.6)])
    _, message, _, _ = format_alert(_alert_row(db), db, sources)
    assert "> M6.6 80 km ENE of Tadine, New Caledonia, depth 10 km [earthquake.usgs.gov]" in message


async def test_push_buttons_open_the_sources(db, sources):
    write_observations(db, [_quake(1000, 6.6)])
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200)

    cfg = NtfyConfig(server="http://n", topic="t", dashboard_url="https://example.org:8001")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await send_ntfy(client, cfg, _alert_row(db), db, sources)
    assert seen["actions"].startswith("view, earthquake.usgs.gov, https://earthquake.usgs.gov/e/1000")
    assert "view, Map, https://www.openstreetmap.org/" in seen["actions"]
    assert seen["click"] == "https://example.org:8001/alert/7"


def test_dashboard_shows_stories(tmp_path, sources):
    from fastapi.testclient import TestClient

    from worldwatch.api.app import create_app
    from worldwatch.db import open_db

    now = int(time.time())
    conn = open_db(tmp_path / "d.db")
    write_observations(conn, [_quake(now - 600, 6.6)])
    conn.execute(
        "INSERT INTO bins (stream_id, cell, scale, bin_start, n, vmin, vmax, vmean) "
        "VALUES ('usgs_seismic', ?, 2, ?, 1, 6.6, 6.6, 6.6)",
        (CELL, now - 1200),
    )
    conn.commit()
    client = TestClient(create_app(tmp_path / "d.db", sources=sources))
    conn.execute(
        "INSERT INTO surprise (stream_id, cell, scale, bin_start, q_value, presence_q, precision, n_obs, tail_index, model_version) VALUES ('usgs_seismic', ?, 2, ?, 0.9996, 1.0, 1.0, 1, NULL, 2)",
        (CELL, now - 1200),
    )
    conn.commit()
    s = client.get("/api/cell", params={"cell": CELL}).json()["streams"][0]
    (story,) = s["stories"]
    assert story["text"].startswith("M6.6 80 km ENE") and story["domain"] == "earthquake.usgs.gov"
    assert [p["text"] for p in s["peak_stories"]] == [story["text"]]  # behind the peak itself
    feats = client.get("/api/activity.geojson").json()["features"]
    assert feats[0]["properties"]["story"].startswith("M6.6")


def test_source_alert_push_names_the_event_and_the_news_around_it(db, sources):
    from worldwatch.alerts.engine import run_alerts

    quake_cell = h3.latlng_to_cell(-21.3, 168.6, 3)
    now = 2_000_000_000
    sig = Observation("usgs_significant", quake_cell, now - 600, 6.6,
                      context={"place": "80 km ENE of Tadine, New Caledonia", "mag": 6.6, "depth_km": 10.0,
                               "url": "https://earthquake.usgs.gov/e/us6000txrf", "_rank": 6.6})
    news = Observation("gdelt_events", quake_cell, now - 300, None,
                       context={"headline": "Strong quake shakes new caledonia", "action": "Public statement",
                                "place": "Noumea, New Caledonia", "mentions": 12,
                                "url": "https://www.rnz.co.nz/quake", "_rank": 12})
    write_observations(db, [sig, news], now=now - 300)
    (aid,) = run_alerts(db, sources, now=now)
    row = db.execute("SELECT * FROM alerts WHERE alert_id = ?", (aid,)).fetchone()
    title, message, priority, _ = format_alert(row, db, sources)
    assert "Confirmed: issued by Significant earthquakes (USGS)" in message and priority == 5
    assert "- Significant earthquakes (USGS): issued by the source @ Coral Sea, off New Caledonia" in message
    assert "> M6.6 80 km ENE of Tadine, New Caledonia, depth 10 km [earthquake.usgs.gov]" in message
    assert "News in the area (context, not evidence):" in message
    assert "Strong quake shakes new caledonia - Public statement, Noumea, New Caledonia [rnz.co.nz]" in message
