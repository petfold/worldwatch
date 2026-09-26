"""Coverage balance and the phase-4 sources (ADR 0002 §G, §H)."""

import dataclasses
import json
import time

import h3
import httpx

from worldwatch.alerts.engine import Anomaly, open_alerts
from worldwatch.config.countries import country_cell
from worldwatch.ingest import parsers
from worldwatch.poll.fetch import get_fetcher
from worldwatch.poll.http import CacheValidators, conditional_get

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">
 <entry><title>Orange Rain Warning issued for Greece - Kriti</title>
  <link title="Kriti" href="https://meteoalarm.org?geocode=EMMA_ID:GR016"/>
  <cap:areaDesc>Kriti</cap:areaDesc><cap:event>Orange Warning</cap:event>
  <cap:onset>2026-09-26T12:00:00+00:00</cap:onset><cap:severity>Severe</cap:severity>
  <cap:identifier>2.49.0.1.300.0.GR.A</cap:identifier></entry>
 <entry><title>Orange Wind Warning issued for Greece - Thessalia</title>
  <cap:areaDesc>Thessalia</cap:areaDesc><cap:event>Orange Warning</cap:event>
  <cap:onset>2026-09-26T12:00:00+00:00</cap:onset><cap:severity>Severe</cap:severity>
  <cap:identifier>2.49.0.1.300.0.GR.B</cap:identifier></entry>
 <entry><title>Yellow Fog Warning</title><cap:event>fog</cap:event>
  <cap:onset>2026-09-26T12:00:00+00:00</cap:onset><cap:severity>Moderate</cap:severity>
  <cap:identifier>2.49.0.1.300.0.GR.C</cap:identifier></entry>
</feed>"""


def _gdacs(level, current="true", fromdate="2026-09-26T10:00:00"):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [166.8, -21.6]},
            "properties": {"eventtype": "EQ", "eventid": 1568194, "episodeid": 1736207,
                           "alertlevel": level, "name": "Earthquake in New Caledonia",
                           "country": "New Caledonia", "fromdate": fromdate, "iscurrent": current}}


# --- MeteoAlarm ---------------------------------------------------------------


def test_meteoalarm_keeps_orange_red_per_country_with_titles(sources):
    cfg = sources["meteoalarm_warnings"]
    obs = parsers.parse([{"target": "greece", "cc": "GR", "status": 200, "text": ATOM},
                         {"target": "france", "cc": "FR", "status": 200, "text": ""}], cfg)
    assert len(obs) == 2  # the yellow fog warning is dropped
    assert {o.cell for o in obs} == {country_cell("GR", 3)}
    assert len({o.ts for o in obs}) == 2  # same onset, distinct warnings
    assert obs[0].context["title"].startswith("Orange Rain Warning")
    assert obs[0].context["level"] == "orange"


async def test_multi_get_isolates_a_failing_feed(sources):
    cfg = dataclasses.replace(sources["meteoalarm_warnings"],
                              fetch={"kind": "multi_get", "pause_seconds": 0,
                                     "targets": {"greece": "GR", "norway": "NO"}})

    def handler(request):
        if "norway" in str(request.url):
            return httpx.Response(503)
        return httpx.Response(200, text=ATOM)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        r = await get_fetcher(cfg)(client, cfg, CacheValidators(), 0)
    status = {e["target"]: e["status"] for e in r.payload}
    assert status == {"greece": 200, "norway": 503}
    assert len(parsers.parse(r.payload, cfg)) == 2


# --- GDACS ----------------------------------------------------------------------


def test_gdacs_current_events_and_escalation_is_new(sources):
    cfg = sources["gdacs_orange"]
    past = parsers.parse({"features": [_gdacs("Orange", current="false")]}, cfg)
    assert past == []
    (orange,) = parsers.parse({"features": [_gdacs("Orange")]}, cfg)
    (red,) = parsers.parse({"features": [_gdacs("Red")]}, sources["gdacs_red"])
    assert red.cell == orange.cell and red.ts != orange.ts  # Orange → Red alerts again
    assert orange.context["url"] == "https://www.gdacs.org/report.aspx?eventid=1568194&eventtype=EQ"
    assert orange.context["type"] == "Earthquake"


async def test_no_content_is_an_empty_result_not_an_error():
    def handler(request):
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        r = await conditional_get(client, "https://example.org/x", CacheValidators())
    assert r.status == 204 and r.payload is None


def test_gdacs_red_wakes_orange_does_not(sources):
    assert sources["gdacs_red"].extra["alerts"]["severity"] >= 0.9
    assert sources["gdacs_orange"].extra["alerts"]["severity"] < 0.9


# --- coverage balance -------------------------------------------------------------


def test_uniform_magnitude_detects_small_quakes_are_context(sources):
    assert sources["usgs_seismic"].extra["alerts"]["role"] == "context"
    assert sources["emsc_seismic"].extra["alerts"]["role"] == "context"
    assert "4.5_hour" in sources["usgs_m45"].endpoint
    assert sources["emsc_m45"].parse["filter_min_mag"] == 4.5
    assert "severity=Severe,Extreme" in sources["nws_severe_alerts"].endpoint
    assert sources["safecast_radiation"].status == "retired"


def test_a_stream_contributes_at_most_three_cells_to_an_alert(db, sources):
    quake = dataclasses.replace(sources["usgs_m45"], stream_id="q")
    traffic = dataclasses.replace(sources["cf_radar_netflows_gb"], stream_id="t")
    region = h3.latlng_to_cell(54.0, -2.5, 2)
    kids = h3.cell_to_children(region, 3)
    cands = [Anomaly("q", c, -1, 1000, 0.9999, 1, 1, "physical", 0.9999, evidence=10 + i, bin_seconds=300)
             for i, c in enumerate(kids)]
    cands.append(Anomaly("t", region, -1, 1000, 0.9999, 1, 1, "infrastructural", 0.9999, evidence=9, bin_seconds=0))
    (aid,) = open_alerts(db, {"q": quake, "t": traffic}, cands, 2000)
    ev = json.loads(db.execute("SELECT evidence FROM alerts WHERE alert_id = ?", (aid,)).fetchone()[0])
    assert sum(e["stream_id"] == "q" for e in ev) == 3  # the three strongest of seven


def test_coverage_endpoint_marks_thin_and_unmonitored(tmp_path, sources):
    from fastapi.testclient import TestClient

    from worldwatch.api.app import create_app
    from worldwatch.db import open_db

    conn = open_db(tmp_path / "c.db")
    now = int(time.time())
    conn.executemany("INSERT INTO probe_targets (ip, cc, kind, discovered_at) VALUES (?, ?, 'ntp', ?)",
                     [(f"192.0.2.{i}", "DE", now) for i in range(8)] + [("198.51.100.1", "KE", now)])
    conn.commit()
    c = TestClient(create_app(tmp_path / "c.db", sources=sources)).get("/api/coverage").json()
    states = {f["properties"]["cc"]: f["properties"]["state"] for f in c["features"]}
    assert "DE" not in states and states["KE"] == "thin" and states["NG"] == "unmonitored"
    body = json.dumps(c)
    assert "192.0.2." not in body and "198.51.100." not in body  # counts only, never addresses


def test_retired_streams_are_not_shown(tmp_path, sources):
    from fastapi.testclient import TestClient

    from worldwatch.api.app import create_app
    from worldwatch.db import open_db

    conn = open_db(tmp_path / "r.db")
    conn.execute("INSERT INTO bins (stream_id, cell, scale, bin_start, n, vmin, vmax, vmean) "
                 "VALUES ('safecast_radiation', ?, 2, ?, 1, 38, 38, 38)",
                 (h3.latlng_to_cell(35.7, 139.7, 4), int(time.time()) - 600))
    conn.commit()
    client = TestClient(create_app(tmp_path / "r.db", sources=sources))
    assert all(s["stream_id"] != "safecast_radiation" for s in client.get("/api/overview").json()["sources"])
    assert client.get("/api/activity.geojson").json()["features"] == []
