"""Push notifier: alert formatting and ntfy delivery (no real network)."""

import json

import httpx

from worldwatch.api.notify import NtfyConfig, format_alert, notify_alerts, send_ntfy


def _alert(db, alert_id, cell, severity, evidence, status="open"):
    db.execute(
        "INSERT INTO alerts (alert_id, opened_at, status, severity, cell, scale, evidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (alert_id, 1000, status, severity, cell, 0, json.dumps(evidence)),
    )
    db.commit()
    return db.execute("SELECT * FROM alerts WHERE alert_id = ?", (alert_id,)).fetchone()


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_format_value_alert(db):
    row = _alert(
        db,
        1,
        "8226",
        0.95,
        [
            {"stream_id": "quake", "modality": "physical", "q_value": 0.999, "presence_q": 1.0},
            {"stream_id": "news", "modality": "informational", "q_value": 0.995, "presence_q": 1.0},
        ],
    )
    title, message, priority, tags = format_alert(row)
    assert "8226" in title
    assert "corroborated surprise" in message
    assert "informational, physical" in message  # modalities sorted
    assert priority == 5  # severity >= 0.9
    assert tags == ["rotating_light"]


def test_format_alert_with_observed_context(db, sources):
    import h3

    cell = h3.latlng_to_cell(-21.3, 168.6, 3)
    db.execute(
        "INSERT INTO bins (stream_id, cell, scale, bin_start, n, vmin, vmax, vmean) "
        "VALUES ('usgs_seismic', ?, 2, 5000, 3, 4.1, 6.6, 5.0)",
        (cell,),
    )
    ev = [
        {"stream_id": "usgs_seismic", "modality": "physical", "q_value": 0.9996,
         "presence_q": 1.0, "cell": cell, "scale": 2, "bin_start": 5000},
        {"stream_id": "gdelt_events", "modality": "informational", "q_value": 0.997,
         "presence_q": 1.0, "cell": cell, "scale": 2, "bin_start": 5000},
    ]
    row = _alert(db, 3, h3.cell_to_parent(cell, 2), 0.95, ev)
    title, message, priority, _ = format_alert(row, db, sources)
    assert title.startswith("Worldwatch SEVERE 0.95 - 21.") and title.isascii()
    assert "Earthquakes (M1+) [physical] 1-in-2,500 high: 3 quakes, max M6.6" in message
    assert "News events (GDELT) [informational] 1-in-333 high" in message
    assert "latest bin 1970-01-01 01:23 UTC" in message
    assert priority == 5


async def test_send_ntfy_click_opens_map(db):
    import h3

    cell = h3.latlng_to_cell(35.7, 139.7, 2)
    row = _alert(db, 4, cell, 0.8, [{"stream_id": "q", "modality": "physical", "q_value": 0.999, "presence_q": 1.0}])
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200)

    async with _client(handler) as client:
        await send_ntfy(client, NtfyConfig(server="http://n", topic="t"), row)
    assert "openstreetmap.org" in seen["click"]


async def test_send_ntfy_deep_links_dashboard_when_configured(db):
    import h3

    cell = h3.latlng_to_cell(35.7, 139.7, 2)
    row = _alert(db, 5, cell, 0.8, [{"stream_id": "q", "modality": "physical", "q_value": 0.999, "presence_q": 1.0}])
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200)

    cfg = NtfyConfig(server="http://n", topic="t", dashboard_url="https://example.org:8001")
    async with _client(handler) as client:
        await send_ntfy(client, cfg, row)
    assert seen["click"] == "https://example.org:8001/?alert=5"
    assert seen["actions"].startswith("view, Map, https://www.openstreetmap.org/")


def test_format_silence_alert(db):
    row = _alert(
        db,
        2,
        "_PRESENCE_",
        0.6,
        [
            {"stream_id": "quake", "modality": "physical", "q_value": None, "presence_q": 0.98},
            {"stream_id": "rad", "modality": "physical", "q_value": None, "presence_q": 0.97},
        ],
    )
    title, message, priority, tags = format_alert(row)
    assert "multi-source silence" in message
    assert priority == 3  # 0.6 < 0.7
    assert tags == ["mute"]


async def test_send_ntfy_posts_to_topic(db):
    row = _alert(
        db,
        3,
        "cell",
        0.8,
        [{"stream_id": "s", "modality": "physical", "q_value": 0.99, "presence_q": 1.0}],
    )
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = request.content.decode()
        return httpx.Response(200)

    cfg = NtfyConfig(server="https://ntfy.example", topic="worldwatch", token="secret")
    async with _client(handler) as client:
        assert await send_ntfy(client, cfg, row) is True

    assert seen["url"] == "https://ntfy.example/worldwatch"
    assert seen["headers"]["authorization"] == "Bearer secret"
    assert seen["headers"]["priority"] == "4"
    assert "cell" in seen["body"]


async def test_notify_alerts_noop_when_unconfigured(db, monkeypatch):
    monkeypatch.delenv("WW_NTFY_TOPIC", raising=False)
    _alert(
        db,
        4,
        "c",
        0.9,
        [{"stream_id": "s", "modality": "physical", "q_value": 0.99, "presence_q": 1.0}],
    )
    delivered = await notify_alerts(db, [4], cfg=None)
    assert delivered == 0
    assert (
        db.execute(
            "SELECT COUNT(*) FROM health WHERE component='notify' AND event='unconfigured'"
        ).fetchone()[0]
        == 1
    )


async def test_notify_alerts_delivers(db):
    for i in (5, 6):
        _alert(
            db,
            i,
            f"c{i}",
            0.8,
            [{"stream_id": "s", "modality": "physical", "q_value": 0.99, "presence_q": 1.0}],
        )
    count = {"n": 0}

    def handler(request):
        count["n"] += 1
        return httpx.Response(200)

    cfg = NtfyConfig(server="https://n", topic="t")
    async with _client(handler) as client:
        delivered = await notify_alerts(db, [5, 6], client=client, cfg=cfg)

    assert delivered == 2 and count["n"] == 2
    assert (
        db.execute("SELECT detail FROM health WHERE component='notify' AND event='ok'").fetchone()[
            "detail"
        ]
        == "delivered=2"
    )


async def test_notify_alerts_handles_push_error(db):
    _alert(
        db,
        7,
        "c",
        0.8,
        [{"stream_id": "s", "modality": "physical", "q_value": 0.99, "presence_q": 1.0}],
    )

    def handler(request):
        return httpx.Response(500)

    cfg = NtfyConfig(server="https://n", topic="t")
    async with _client(handler) as client:
        delivered = await notify_alerts(db, [7], client=client, cfg=cfg)

    assert delivered == 0
    assert (
        db.execute(
            "SELECT COUNT(*) FROM health WHERE component='notify' AND event='push_error'"
        ).fetchone()[0]
        == 1
    )


def test_ntfy_config_from_env(monkeypatch):
    monkeypatch.delenv("WW_NTFY_TOPIC", raising=False)
    assert NtfyConfig.from_env() is None
    monkeypatch.setenv("WW_NTFY_TOPIC", "mytopic")
    monkeypatch.setenv("WW_NTFY_SERVER", "https://self.hosted/")
    cfg = NtfyConfig.from_env()
    assert cfg is not None
    assert cfg.topic == "mytopic" and cfg.server == "https://self.hosted"  # trailing / stripped
