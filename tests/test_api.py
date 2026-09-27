"""Dashboard API endpoints (FastAPI TestClient against a temp DB file)."""

import json

import h3
import pytest
from fastapi.testclient import TestClient

from worldwatch.api.app import create_app
from worldwatch.db import open_db

NOW = 2_000_000_000
CELL = h3.latlng_to_cell(38.1, -122.5, 3)


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "api.db"
    conn = open_db(path)
    # geographic surprise rows (one calm, one extreme) + a non-geographic row
    conn.execute(
        "INSERT INTO surprise (stream_id, cell, scale, bin_start, q_value, presence_q, precision, n_obs, tail_index, model_version) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("quake", CELL, 3, NOW - 300, 0.999, 1.0, 1.0, 5, None, 1),
    )
    conn.execute(
        "INSERT INTO surprise (stream_id, cell, scale, bin_start, q_value, presence_q, precision, n_obs, tail_index, model_version) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("quake", CELL, 3, NOW - 600, 0.6, 1.0, 1.0, 2, None, 1),
    )
    conn.execute(
        "INSERT INTO surprise (stream_id, cell, scale, bin_start, q_value, presence_q, precision, n_obs, tail_index, model_version) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("btc", "GLOBAL", 0, NOW - 300, 0.97, 1.0, 1.0, 1, None, 1),
    )
    # a silence row
    conn.execute(
        "INSERT INTO surprise (stream_id, cell, scale, bin_start, q_value, presence_q, precision, n_obs, tail_index, model_version) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("rad", "_PRESENCE_", 0, NOW - 300, None, 0.98, 1.0, 0, None, 1),
    )
    conn.execute(
        "INSERT INTO alerts (alert_id, opened_at, status, severity, cell, scale, evidence) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            1,
            NOW,
            "open",
            0.95,
            CELL,
            3,
            json.dumps(
                [
                    {
                        "stream_id": "quake",
                        "modality": "physical",
                        "q_value": 0.999,
                        "presence_q": 1.0,
                        "cell": CELL,
                    }
                ]
            ),
        ),
    )
    conn.commit()
    conn.close()
    return TestClient(create_app(path, now_fn=lambda: NOW))


def test_index_serves_map(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Worldwatch" in r.text
    assert "maplibre-gl" in r.text


def test_surprise_geojson(client):
    r = client.get("/api/surprise.geojson")
    assert r.status_code == 200
    gj = r.json()
    assert gj["type"] == "FeatureCollection"
    # only the geographic H3 cell appears; GLOBAL and _PRESENCE_ are excluded
    cells = {f["properties"]["cell"] for f in gj["features"]}
    assert cells == {CELL}
    feat = gj["features"][0]
    assert feat["geometry"]["type"] == "Polygon"
    assert feat["properties"]["surprise"] == pytest.approx(0.999)  # max over the cell


def test_silence_endpoint(client):
    r = client.get("/api/silence")
    sources = r.json()["silent_sources"]
    assert [s["stream_id"] for s in sources] == ["rad"]
    assert sources[0]["q"] == pytest.approx(0.98)


def test_timeline(client):
    r = client.get(f"/api/timeline?stream=quake&cell={CELL}&scale=3")
    pts = r.json()["points"]
    assert len(pts) == 2
    assert pts[0]["bin_start"] < pts[1]["bin_start"]  # ascending
    assert pts[1]["q_value"] == pytest.approx(0.999)


def test_alerts_feed(client):
    r = client.get("/api/alerts")
    alerts = r.json()["alerts"]
    assert len(alerts) == 1
    assert alerts[0]["evidence"][0]["modality"] == "physical"  # parsed to a list


def test_alert_detail_404(client):
    assert client.get("/api/alerts/999").status_code == 404


def test_label_alert(client):
    r = client.post("/api/alerts/1/label", json={"label": "true"})
    assert r.status_code == 200
    assert client.get("/api/alerts/1").json()["label"] == "true"


def test_label_invalid(client):
    assert client.post("/api/alerts/1/label", json={"label": "bogus"}).status_code == 422


def test_label_missing_alert(client):
    assert client.post("/api/alerts/999/label", json={"label": "true"}).status_code == 404


@pytest.fixture
def rich_client(tmp_path, sources):
    """Real stanzas (labels/units) + bins and health rows, as the VPS has them."""
    path = tmp_path / "rich.db"
    conn = open_db(path)
    quake_cell = h3.latlng_to_cell(-21.3, 168.6, 3)
    for sid in ("usgs_seismic", "btc_usd", "bfs_odl_gamma"):
        conn.execute(
            "INSERT INTO sources (stream_id, class, modality, flavor, status, created_at) "
            "VALUES (?, 'x', ?, ?, 'nursery', 0)",
            (sid, sources[sid].modality, sources[sid].flavor),
        )
    rows = [
        ("usgs_seismic", quake_cell, 2, NOW - 3600, 3, 4.0, 6.6, 5.0),
        ("usgs_seismic", CELL, 2, NOW - 2400, 1, 2.0, 2.0, 2.0),
        ("btc_usd", "GLOBAL", 2, NOW - 3600, 5, 11.3, 11.4, 11.338),
        ("btc_usd", "GLOBAL", 2, NOW - 2400, 5, 11.3, 11.4, 11.34),
        ("btc_usd", "GLOBAL", 2, NOW - 1200, 5, 11.3, 11.4, 11.35),
        ("bfs_odl_gamma", CELL, 19, NOW - 90 * 86400, 1, -2.3, -2.3, -2.3),  # stale feed (log µSv/h)
    ]
    conn.executemany(
        "INSERT INTO bins (stream_id, cell, scale, bin_start, n, vmin, vmax, vmean) "
        "VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.execute(
        "INSERT INTO surprise (stream_id, cell, scale, bin_start, q_value, presence_q, precision, n_obs, tail_index, model_version) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("usgs_seismic", quake_cell, 2, NOW - 3600, 0.999, 1.0, 1.0, 3, None, 1),
    )
    conn.execute(
        "INSERT INTO surprise (stream_id, cell, scale, bin_start, q_value, presence_q, precision, n_obs, tail_index, model_version) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("btc_usd", "GLOBAL", 2, NOW - 1200, 0.6, 1.0, 1.0, 5, None, 1),
    )
    conn.executemany(
        "INSERT INTO health VALUES (?,?,?,?)",
        [
            ("usgs_seismic", NOW - 120, "ok", "rows=3"),
            ("btc_usd", NOW - 200, "ok", "rows=1"),
            ("bfs_odl_gamma", NOW - 900, "ok", "rows=0"),
            ("bfs_odl_gamma", NOW - 60, "http_error", "503 Service Unavailable"),
        ],
    )
    conn.commit()
    conn.close()
    return TestClient(create_app(path, now_fn=lambda: NOW, sources=sources))


def _src(overview, sid):
    return next(s for s in overview["sources"] if s["stream_id"] == sid)


def test_overview_describes_calm_data(rich_client):
    o = rich_client.get("/api/overview").json()
    assert o["open_alerts"] == 0
    quakes = _src(o, "usgs_seismic")
    assert quakes["label"] == "Earthquakes, all magnitudes (USGS)" and quakes["state"] == "ok"
    assert quakes["latest"] == "4 quakes in 2 cells; largest M6.6 @ 20.9S 169.0E"
    assert quakes["peak"]["rarity"] == "rare, 1-in-1,000 high"
    btc = _src(o, "btc_usd")
    assert btc["latest"].startswith("$") and len(btc["spark"]) == 3
    assert btc["now_rarity"] == "typical"


def test_overview_shows_stale_feed_and_error(rich_client):
    rad = _src(rich_client.get("/api/overview").json(), "bfs_odl_gamma")
    assert rad["state"] == "error"
    assert rad["last_error"]["event"] == "http_error"
    assert rad["latest"] == "0.100 µSv/h"  # newest data, even though outside the look-back
    assert rad["latest_at"] < NOW - 80 * 86400


def test_activity_points_carry_hover_details(rich_client):
    feats = rich_client.get("/api/activity.geojson").json()["features"]
    quake = next(f for f in feats if f["properties"]["n"] == 3)
    assert quake["geometry"]["type"] == "Point"
    assert quake["properties"]["latest"] == "3 quakes, max M6.6"
    assert quake["properties"]["label"] == "Earthquakes, all magnitudes (USGS)"
    assert all(f["properties"]["cell"] != "GLOBAL" for f in feats)


def test_cell_detail(rich_client):
    cell = h3.latlng_to_cell(-21.3, 168.6, 3)
    c = rich_client.get("/api/cell", params={"cell": cell}).json()
    (s,) = c["streams"]
    assert s["latest"] == "3 quakes, max M6.6" and s["peak"] == "rare, 1-in-1,000 high"


def test_alert_carries_readable_text(client):
    a = client.get("/api/alerts/1").json()
    assert a["title"] == "WW Unconfirmed: quake - United States"
    assert "Unconfirmed: one kind of measurement so far" in a["text"]


def test_about_page_is_served_and_linked(client):
    page = client.get("/about")
    assert page.status_code == 200 and "Opting out" in page.text and "158.220.117.131" in page.text
    assert 'href="/about"' in client.get("/").text


# --- "only in view" and where each row takes the map ------------------------------------


def test_overview_gives_every_row_a_place(rich_client):
    o = rich_client.get("/api/overview").json()
    by = {s["stream_id"]: s for s in o["sources"]}
    assert by["btc_usd"]["global"] is True and by["btc_usd"]["extent"] is None
    q = by["usgs_seismic"]
    assert q["global"] is False and q["center"] is not None  # its peak is unusual (1-in-1,000)
    w, s_, e, n = q["extent"]
    assert w < e and s_ < n


def test_view_filter_keeps_global_streams_and_hides_what_is_elsewhere(rich_client):
    europe = rich_client.get("/api/overview", params={"bbox": "-12,34,32,62"}).json()
    ids = {s["stream_id"] for s in europe["sources"]}
    assert "btc_usd" in ids  # not tied to a place: always listed
    assert "usgs_seismic" not in ids and europe["out_of_view"] >= 1
    pacific = rich_client.get("/api/overview", params={"bbox": "160,-30,200,0"}).json()  # across ±180
    assert "usgs_seismic" in {s["stream_id"] for s in pacific["sources"]}


def test_view_filter_locates_within_the_view(rich_client):
    # usgs_seismic's unusual peak is near Vanuatu; zoomed on California the row
    # must take the map to what it reported there, not fly across the Pacific
    o = rich_client.get("/api/overview", params={"bbox": "-126,34,-118,42"}).json()
    q = _src(o, "usgs_seismic")
    assert q["center"] is None  # no unusual peak in view
    w, s_, e, n = q["extent"]
    assert -126 < w <= e < -118 and 34 < s_ <= n < 42


def test_hexagon_across_the_antimeridian_stays_small():
    from worldwatch.api.app import _cell_polygon

    fiji = h3.latlng_to_cell(-17.8, 180.0, 3)  # straddles ±180°
    lons = [p[0] for p in _cell_polygon(fiji)]
    assert max(lons) - min(lons) < 10  # not a band around the whole world


def test_parse_bbox_normalizes_world_copies():
    from worldwatch.api.app import in_view, parse_bbox

    assert parse_bbox("-200,-80,200,80") == (-180.0, -80.0, 180.0, 80.0)  # whole width in view
    v = parse_bbox("170,-10,190,10")  # MapLibre past the antimeridian
    assert v == (170.0, -10.0, -170.0, 10.0)
    assert in_view(h3.latlng_to_cell(0, 179, 3), v) and in_view(h3.latlng_to_cell(0, -175, 3), v)
    assert not in_view(h3.latlng_to_cell(0, 0, 3), v)
    assert in_view("GLOBAL", v) and parse_bbox("nonsense") is None


def test_alerts_filtered_by_view(client):
    assert len(client.get("/api/alerts", params={"bbox": "-125,35,-120,40"}).json()["alerts"]) == 1
    assert client.get("/api/alerts", params={"bbox": "0,40,10,50"}).json()["alerts"] == []


def test_alert_report_page(client):
    r = client.get("/alert/1")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    page = r.text
    assert "WW Unconfirmed: quake - United States" in page
    for part in ("Certainty", "Surprise", "Severity", "Reach", "History", "Signals", "Opened as"):
        assert part in page
    assert client.get("/alert/999").status_code == 404


def test_alert_report_escapes_what_sources_say(client, tmp_path):
    from worldwatch.api.report import _link

    assert "<script>" not in _link("javascript:alert(1)", "<script>x</script>")
    assert 'href="https://a.b/?q=&quot;x&quot;"' in _link('https://a.b/?q="x"', "t")
