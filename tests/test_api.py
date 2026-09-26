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
        "INSERT INTO surprise VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("quake", CELL, 3, NOW - 300, 0.999, 1.0, 1.0, 5, None, 1),
    )
    conn.execute(
        "INSERT INTO surprise VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("quake", CELL, 3, NOW - 600, 0.6, 1.0, 1.0, 2, None, 1),
    )
    conn.execute(
        "INSERT INTO surprise VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("btc", "GLOBAL", 0, NOW - 300, 0.97, 1.0, 1.0, 1, None, 1),
    )
    # a silence row
    conn.execute(
        "INSERT INTO surprise VALUES (?,?,?,?,?,?,?,?,?,?)",
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
    for sid in ("usgs_seismic", "btc_usd", "safecast_radiation"):
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
        ("safecast_radiation", CELL, 19, NOW - 90 * 86400, 1, 38, 38, 38),  # stale feed
    ]
    conn.executemany(
        "INSERT INTO bins (stream_id, cell, scale, bin_start, n, vmin, vmax, vmean) "
        "VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.execute(
        "INSERT INTO surprise VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("usgs_seismic", quake_cell, 2, NOW - 3600, 0.999, 1.0, 1.0, 3, None, 1),
    )
    conn.execute(
        "INSERT INTO surprise VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("btc_usd", "GLOBAL", 2, NOW - 1200, 0.6, 1.0, 1.0, 5, None, 1),
    )
    conn.executemany(
        "INSERT INTO health VALUES (?,?,?,?)",
        [
            ("usgs_seismic", NOW - 120, "ok", "rows=3"),
            ("btc_usd", NOW - 200, "ok", "rows=1"),
            ("safecast_radiation", NOW - 900, "ok", "rows=0"),
            ("safecast_radiation", NOW - 60, "http_error", "503 Service Unavailable"),
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
    assert quakes["label"] == "Earthquakes (M1+)" and quakes["state"] == "ok"
    assert quakes["latest"] == "4 quakes in 2 cells; largest M6.6 @ 20.9S 169.0E"
    assert quakes["peak"]["rarity"] == "rare, 1-in-1,000 high"
    btc = _src(o, "btc_usd")
    assert btc["latest"].startswith("$") and len(btc["spark"]) == 3
    assert btc["now_rarity"] == "typical"


def test_overview_shows_stale_feed_and_error(rich_client):
    rad = _src(rich_client.get("/api/overview").json(), "safecast_radiation")
    assert rad["state"] == "error"
    assert rad["last_error"]["event"] == "http_error"
    assert rad["latest"] == "38 cpm"  # newest data, even though outside the look-back
    assert rad["latest_at"] < NOW - 80 * 86400


def test_activity_points_carry_hover_details(rich_client):
    feats = rich_client.get("/api/activity.geojson").json()["features"]
    quake = next(f for f in feats if f["properties"]["n"] == 3)
    assert quake["geometry"]["type"] == "Point"
    assert quake["properties"]["latest"] == "3 quakes, max M6.6"
    assert quake["properties"]["label"] == "Earthquakes (M1+)"
    assert all(f["properties"]["cell"] != "GLOBAL" for f in feats)


def test_cell_detail(rich_client):
    cell = h3.latlng_to_cell(-21.3, 168.6, 3)
    c = rich_client.get("/api/cell", params={"cell": cell}).json()
    (s,) = c["streams"]
    assert s["latest"] == "3 quakes, max M6.6" and s["peak"] == "rare, 1-in-1,000 high"


def test_alert_carries_readable_text(client):
    a = client.get("/api/alerts/1").json()
    assert a["title"].startswith("Worldwatch SEVERE 0.95")
    assert "corroborated surprise" in a["text"]
