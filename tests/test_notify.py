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
    assert "Confirmed: 2 independent kinds of measurement agree" in message
    assert "informational" not in message  # the sources by name, not their category
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
    assert title == "WW Confirmed: Earthquakes, all magnitudes (USGS) + 1 - Coral Sea, off New Caledonia"
    assert "- Earthquakes, all magnitudes (USGS): unusually high (1 in 2,500): 3 quakes, max M6.6 @ Coral Sea" in message
    assert "- News events (GDELT): unusually high (1 in 333) @ Coral Sea" in message
    assert "Latest data: 1970-01-01 01:23 UTC" in message
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
    assert seen["click"] == "https://example.org:8001/alert/5"
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
    assert "sources that stopped reporting" in message
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

    cfg = NtfyConfig(server="https://n", topic="t", min_score=0)  # delivery, not the budget
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

    cfg = NtfyConfig(server="https://n", topic="t", min_score=0)  # delivery, not the budget
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


# --- limits: the rate cap with one digest, and silent pushes (2026-09-27: 387 pushes in a day)


def _value_alert(db, alert_id, q, modalities=("physical",), opened_at=10_000):
    ev = [{"stream_id": f"s{m}", "cell": f"c{k}", "modality": m, "q_value": q, "presence_q": 1.0}
          for k, m in enumerate(modalities)]
    db.execute("INSERT INTO alerts (alert_id, opened_at, status, severity, cell, scale, evidence) "
               "VALUES (?, ?, 'open', 0.85, '8226', 0, ?)", (alert_id, opened_at, json.dumps(ev)))
    db.commit()


async def test_only_the_most_serious_alerts_are_pushed(db):
    posts = []

    def handler(request):
        posts.append(int(request.headers["priority"]))
        return httpx.Response(200)

    # scores: -log10 of the two-sided tail p; the floor is 8
    for i, q in enumerate([1 - 1e-3, 1 - 1e-5, 1 - 1e-7, 1 - 1e-9, 1 - 1e-10], start=1):
        _value_alert(db, i, q)
    cfg = NtfyConfig(server="http://n", topic="t")
    async with _client(handler) as client:
        assert await notify_alerts(db, [1, 2, 3, 4, 5], client=client, cfg=cfg, now=10_000) == 2
    assert posts == [3, 3]  # the two above the floor, at normal priority: no waking


async def test_the_daily_cap_holds_even_serious_alerts(db):
    for i in range(1, 7):
        _value_alert(db, i, 1 - 1e-10)
    cfg = NtfyConfig(server="http://n", topic="t", per_day=2)
    async with _client(lambda r: httpx.Response(200)) as client:
        assert await notify_alerts(db, list(range(1, 7)), client=client, cfg=cfg, now=10_000) == 4
        assert await notify_alerts(db, [], client=client, cfg=cfg, now=10_000) == 0


async def test_the_budget_threshold_rises_on_a_busy_week(db):
    """A week of 30 strong alerts: only about per_day × 7 of the strongest can be pushed."""
    for i in range(1, 31):
        _value_alert(db, i, 1 - 10.0 ** -(9 + i / 10), opened_at=10_000 - i * 3600)
    from worldwatch.api.notify import _budget_threshold

    cfg = NtfyConfig(server="http://n", topic="t", per_day=2)
    thr = _budget_threshold(db, cfg, None, now=10_000)
    assert 9 < thr < 12  # the 14th highest of scores 9.1 .. 12.0, above the floor of 8


async def test_extreme_alerts_wake_only_when_confirmed_and_rarely(db):
    posts = []

    def handler(request):
        posts.append(int(request.headers["priority"]))
        return httpx.Response(200)

    _value_alert(db, 1, 1 - 1e-12)  # one modality, score 12: pushed, not waking
    _value_alert(db, 2, 1 - 1e-9, modalities=("physical", "infrastructural"))  # 2 x 2 x 9 = 36: extreme
    _value_alert(db, 3, 1 - 1e-9, modalities=("physical", "infrastructural"))  # extreme, but the week's one is used
    cfg = NtfyConfig(server="http://n", topic="t")
    async with _client(handler) as client:
        await notify_alerts(db, [1, 2, 3], client=client, cfg=cfg, now=10_000)
    assert posts == [3, 5, 4]  # the third as confirmed: no waking


async def test_silent_until_caps_the_priority(db):
    seen = []

    def handler(request):
        seen.append(int(request.headers["priority"]))
        return httpx.Response(200)

    row = _alert(db, 1, "8226", 0.95, [{"stream_id": "quake", "modality": "physical", "q_value": 0.999}])
    async with _client(handler) as client:
        await send_ntfy(client, NtfyConfig(server="http://n", topic="t", silent_until=2**40), row)
        await send_ntfy(client, NtfyConfig(server="http://n", topic="t", silent_until=1), row)
    assert seen == [2, 5]  # silent (no sound, no vibration) until the date, then as usual


def test_silent_until_parses_dates_and_epochs(monkeypatch):
    monkeypatch.setenv("WW_NTFY_TOPIC", "t")
    monkeypatch.setenv("WW_PUSH_SILENT_UNTIL", "2026-10-04")
    assert NtfyConfig.from_env().silent_until == 1791072000  # 2026-10-04T00:00Z
    monkeypatch.setenv("WW_PUSH_SILENT_UNTIL", "1791072000")
    assert NtfyConfig.from_env().silent_until == 1791072000


async def test_an_alert_is_pushed_early_then_again_as_it_is_confirmed(db):
    seen = []

    def handler(request):
        seen.append((int(request.headers["priority"]), request.headers["title"].split(":")[0]))
        return httpx.Response(200)

    _value_alert(db, 1, 1 - 1e-11)  # one modality, score 10.7: unconfirmed
    cfg = NtfyConfig(server="http://n", topic="t", per_day=1)
    async with _client(handler) as client:
        assert await notify_alerts(db, [1], client=client, cfg=cfg, now=10_000) == 1
        assert await notify_alerts(db, [1], client=client, cfg=cfg, now=10_060) == 0  # same stage: once
        for i in range(2, 4):  # the daily cap (2 x 1) is used up
            _value_alert(db, i, 1 - 1e-12)
        assert await notify_alerts(db, [2, 3], client=client, cfg=cfg, now=10_100) == 1
        ev = json.loads(db.execute("SELECT evidence FROM alerts WHERE alert_id = 1").fetchone()[0])
        ev.append({"stream_id": "net", "cell": "c9", "modality": "infrastructural", "q_value": 1 - 1e-3,
                   "presence_q": 1.0})
        db.execute("UPDATE alerts SET evidence = ?, stage = 2 WHERE alert_id = 1", (json.dumps(ev),))
        db.commit()
        # escalated (2 modalities, 2 x 13.4 >= 15): re-pushed despite the cap, and it wakes
        assert await notify_alerts(db, [1], client=client, cfg=cfg, now=10_200) == 1
    assert seen == [(3, "WW Unconfirmed"), (3, "WW Unconfirmed"), (5, "WW EXTREME (update)")]
    assert [tuple(r) for r in db.execute("SELECT alert_id, kind, stage FROM push_log")] == [
        (1, "alert", 0), (2, "alert", 0), (1, "extreme", 2)]


async def test_a_held_alert_is_pushed_when_confirmation_lifts_it_into_the_budget(db):
    seen = []

    def handler(request):
        seen.append(int(request.headers["priority"]))
        return httpx.Response(200)

    _value_alert(db, 1, 1 - 1e-5)  # score 4.7: below the floor, held
    cfg = NtfyConfig(server="http://n", topic="t")
    async with _client(handler) as client:
        assert await notify_alerts(db, [1], client=client, cfg=cfg, now=10_000) == 0
        ev = json.loads(db.execute("SELECT evidence FROM alerts WHERE alert_id = 1").fetchone()[0])
        ev.append({"stream_id": "net", "cell": "c9", "modality": "infrastructural", "q_value": 1 - 1e-2,
                   "presence_q": 1.0})
        db.execute("UPDATE alerts SET evidence = ?, stage = 1 WHERE alert_id = 1", (json.dumps(ev),))
        db.commit()
        assert await notify_alerts(db, [1], client=client, cfg=cfg, now=10_100) == 1  # 2 x 6.4 = 12.8
    assert seen == [4]  # confirmed: priority 4, not waking


async def test_an_early_alert_skips_the_ranking_but_not_the_daily_cap(db):
    ev = [{"stream_id": "q", "cell": "c", "modality": "physical", "q_value": 1 - 1e-7, "presence_q": 1.0,
           "kind": "provisional"}]  # score 6.7: below the floor of 8, but early
    for i in (1, 2, 3):
        db.execute("INSERT INTO alerts (alert_id, opened_at, status, severity, cell, scale, evidence) "
                   "VALUES (?, 10000, 'open', 0.6, '8226', 0, ?)", (i, json.dumps(ev)))
    db.commit()
    cfg = NtfyConfig(server="http://n", topic="t", per_day=1)
    async with _client(lambda r: httpx.Response(200)) as client:
        assert await notify_alerts(db, [1, 2, 3], client=client, cfg=cfg, now=10_000) == 2


# --- reach: only what can affect the operator may wake them


def _reach_sources(sources):
    import dataclasses

    def with_alerts(sid, modality, pol):
        base = sources["usgs_m45"]
        return dataclasses.replace(base, stream_id=sid, modality=modality, extra={**base.extra, "alerts": pol})

    return {
        "quake": with_alerts("quake", "physical", {"reach_km": 300}),
        "net": with_alerts("net", "infrastructural", {"reach_km": 300}),
        "wiki": with_alerts("wiki", "informational", {}),
        "rad": with_alerts("rad", "physical", {"reach_km": 500, "global_sensors": 5, "extreme": True}),
    }


DUBAI = ((25.2, 55.3),)


def _cell(lat, lng, res=3):
    import h3

    return h3.latlng_to_cell(lat, lng, res)


def test_in_reach(sources):
    from worldwatch.api.notify import in_reach

    src = _reach_sources(sources)
    tokyo, bandar_abbas = _cell(35.7, 139.7), _cell(27.2, 56.3)  # 12 000 km, 250 km from Dubai
    assert not in_reach([{"stream_id": "quake", "cell": tokyo}], src, DUBAI)
    assert in_reach([{"stream_id": "quake", "cell": bandar_abbas}], src, DUBAI)
    assert in_reach([{"stream_id": "quake", "cell": tokyo}], src, ())  # no home: everything
    assert not in_reach([{"stream_id": "wiki", "cell": bandar_abbas}], src, DUBAI)  # attention has no reach
    europe = [{"stream_id": "rad", "cell": _cell(50 + i / 10, 10 + i / 10, 10)} for i in range(5)]
    assert not in_reach(europe[:4], src, DUBAI)  # a few stations: local
    assert in_reach(europe, src, DUBAI)  # many at once: a release, worldwide


async def test_an_extreme_event_far_away_does_not_wake(db, sources):
    seen = []

    def handler(request):
        seen.append((int(request.headers["priority"]), request.headers["title"].split(":")[0]))
        return httpx.Response(200)

    src = _reach_sources(sources)
    for aid, (lat, lng) in ((1, (35.7, 139.7)), (2, (27.2, 56.3))):  # Tokyo, then near Dubai
        ev = [{"stream_id": s, "cell": _cell(lat, lng), "modality": m, "q_value": 1 - 1e-9, "presence_q": 1.0}
              for s, m in (("quake", "physical"), ("net", "infrastructural"))]  # 2 x 17.4: extreme
        db.execute("INSERT INTO alerts (alert_id, opened_at, status, severity, cell, scale, evidence, stage) "
                   "VALUES (?, 10000, 'open', 0.95, ?, 0, ?, 2)", (aid, _cell(lat, lng), json.dumps(ev)))
    db.commit()
    cfg = NtfyConfig(server="http://n", topic="t", home=DUBAI)
    async with _client(handler) as client:
        assert await notify_alerts(db, [1, 2], client=client, cfg=cfg, sources=src, now=10_000) == 2
    assert seen == [(3, "WW EXTREME, far away"), (5, "WW EXTREME")]  # Tokyo: news; the far one keeps the week's wake-up


def test_home_parses_one_or_several_places(monkeypatch):
    monkeypatch.setenv("WW_NTFY_TOPIC", "t")
    monkeypatch.setenv("WW_HOME", "25.2,55.3; 47.5, 19.0")
    assert NtfyConfig.from_env().home == ((25.2, 55.3), (47.5, 19.0))
    monkeypatch.delenv("WW_HOME")
    assert NtfyConfig.from_env().home == ()


def test_an_extreme_stanza_confirms_only_on_its_own_terms(sources):
    from worldwatch.alerts.engine import alert_score

    src = _reach_sources(sources)
    src["rad"].extra["alerts"].update(min_sensors=2, q_tail=1e-4)
    weak = [{"stream_id": "rad", "cell": f"c{i}", "modality": "physical", "q_value": 0.03} for i in range(3)]
    one = [{"stream_id": "rad", "cell": "c0", "modality": "physical", "q_value": 1 - 1e-6}]
    two = one + [{"stream_id": "rad", "cell": "c1", "modality": "physical", "q_value": 1e-5}]
    assert not alert_score(weak, src)[2]  # low readings, merged in: nothing confirmed
    assert not alert_score(one, src)[2]  # one station: could be the detector
    assert alert_score(two, src)[2]  # two independent stations beyond the network's threshold
