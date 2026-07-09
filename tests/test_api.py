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
