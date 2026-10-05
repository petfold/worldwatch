"""Resource use as data: per-source bytes, the slice sample, the report and its warnings."""

import asyncio
import gzip

import httpx
import pytest
from fastapi.testclient import TestClient

from worldwatch import usage
from worldwatch.api.app import create_app
from worldwatch.db import open_db

DAY = 86400
NOW = 1791200000  # 2026-10-05 08:13 UTC


@pytest.fixture(autouse=True)
def _clean_counts():
    usage._counts.clear()
    yield
    usage._counts.clear()


async def test_transport_charges_each_task_its_own_bytes():
    big = gzip.compress(b"x" * 100_000)  # compresses to a few hundred bytes on the wire

    def handler(request):
        if request.url.path == "/big":
            return httpx.Response(200, content=big, headers={"Content-Encoding": "gzip"})
        return httpx.Response(200, content=b"y" * 5000)

    transport = usage.CountingTransport(httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=transport) as client:

        async def poll(component, path, times):
            usage.tag(component)
            for _ in range(times):
                resp = await client.post(f"https://example.test{path}", content=b"q" * 100)
                assert len(resp.content) in (100_000, 5000)  # decoded for the caller

        await asyncio.gather(poll("goes19_fire", "/big", 2), poll("s5p_no2", "/small", 3))

    goes, s5p = usage._counts["goes19_fire"], usage._counts["s5p_no2"]
    assert goes[2] == 2 and s5p[2] == 3  # requests
    assert len(big) * 2 < goes[0] < len(big) * 2 + 400  # compressed body + headers, not 200 kB
    assert 15_000 < s5p[0] < 15_000 + 600
    assert goes[1] > 200 and s5p[1] > 300  # bodies + request lines and headers
    assert "other" not in usage._counts


def test_flush_adds_to_the_day(tmp_path):
    conn = open_db(tmp_path / "u.db")
    usage.add("a", bytes_in=1000, requests=1)
    usage.flush(conn, NOW)
    usage.add("a", bytes_in=500, bytes_out=20, requests=2)
    usage.flush(conn, NOW + 600)
    usage.add("a", bytes_in=7)
    usage.flush(conn, NOW + DAY)  # the next day: a new row
    rows = [tuple(r) for r in conn.execute("SELECT component, day, bytes_in, bytes_out, requests FROM usage ORDER BY day")]
    day = NOW // DAY * DAY
    assert rows == [("a", day, 1500, 20, 3), ("a", day + DAY, 7, 0, 0)]
    assert usage._counts == {}


def _fake_slice(tmp_path, monkeypatch, current, peak, cap, cpu_usec, net=(None, None)):
    cg = tmp_path / "cg"
    cg.mkdir(exist_ok=True)
    (cg / "memory.current").write_text(f"{current}\n")
    (cg / "memory.peak").write_text(f"{peak}\n")
    (cg / "memory.max").write_text(f"{cap}\n")
    (cg / "cpu.stat").write_text(f"usage_usec {cpu_usec}\nuser_usec 1\n")
    monkeypatch.setattr(usage, "_slice_dir", lambda name: cg)
    monkeypatch.setattr(usage, "_ip_accounting", lambda name: net)


def test_sample_reads_the_slice_and_disk(tmp_path, monkeypatch):
    db = tmp_path / "u.db"
    conn = open_db(db)
    _fake_slice(tmp_path, monkeypatch, 1_900_000_000, 3_200_000_000, 3_774_873_600, 5_000_000, (10, 20))
    row = usage.sample(conn, NOW, db)
    assert row["mem_current"] == 1_900_000_000 and row["mem_max"] == 3_774_873_600
    assert row["cpu_usec"] == 5_000_000 and (row["net_in"], row["net_out"]) == (10, 20)
    assert row["disk_free"] > 0 and row["db_bytes"] > 0
    assert conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0] == 1


def test_sample_outside_a_slice_records_what_it_can(tmp_path, monkeypatch):
    db = tmp_path / "u.db"
    conn = open_db(db)
    monkeypatch.setattr(usage, "_slice_dir", lambda name: None)
    row = usage.sample(conn, NOW, db)
    assert row["mem_current"] is None and row["net_in"] is None
    assert row["disk_free"] > 0


def _samples(conn, monkeypatch, tmp_path, *, mem=1_000_000_000, net_per_hour=50_000_000):
    """25 hourly samples: CPU at 10% of a core, the given download rate."""
    for h in range(25):
        _fake_slice(tmp_path, monkeypatch, mem, mem, 3_774_873_600, h * 360_000_000,
                    (h * net_per_hour, h * 1_000_000))
        usage.sample(conn, NOW - (24 - h) * 3600, tmp_path / "u.db")


def test_report_rates_and_ranking(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "u.db")
    _samples(conn, monkeypatch, tmp_path)
    day = NOW // DAY * DAY
    for comp, b in (("goes19_fire", 200_000_000), ("night_lights_nrt_h18v04", 45_000_000), ("usgs", 1_000_000)):
        conn.execute("INSERT INTO usage VALUES (?, ?, ?, 0, 10)", (comp, day - DAY, b))
    rep = usage.report(conn, NOW)

    assert rep["cpu_percent_of_one_core"] == 10.0
    assert rep["net_in_mb_day"] == 1200.0  # 50 MB an hour
    y = rep["traffic"]["yesterday"]
    assert [s["component"] for s in y["sources"]] == ["goes19_fire", "night_lights_nrt_h18v04", "usgs"]
    assert y["sources"][0]["share"] == round(200 / 246, 3)
    # GOES: 81% of the day and 200 MB > the 100 MB floor → disproportionate
    assert [w["kind"] for w in rep["warnings"]] == ["source:goes19_fire"]
    text = "\n".join(usage.summary_lines(rep))
    assert "goes19_fire: 200.0 MB (81%)" in text and "**goes19_fire downloads" in text


def test_report_warns_on_memory_disk_and_budget(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "u.db")
    monkeypatch.setenv("WW_DISK_MIN_FREE_GB", "1000000")  # any real disk is "low"
    _samples(conn, monkeypatch, tmp_path, mem=3_300_000_000, net_per_hour=200_000_000)
    kinds = [w["kind"] for w in usage.report(conn, NOW)["warnings"]]
    assert kinds == ["memory", "disk", "bandwidth"]  # 87% of the cap; 4.8 GB/day > 3 GB


def test_small_sources_are_never_disproportionate(tmp_path):
    conn = open_db(tmp_path / "u.db")
    day = NOW // DAY * DAY
    conn.execute("INSERT INTO usage VALUES ('tiny', ?, 5000000, 0, 3)", (day - DAY,))  # 100% of 5 MB
    assert usage.report(conn, NOW)["warnings"] == []


async def test_check_records_every_warning_and_pushes_once_a_day(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "u.db")
    monkeypatch.setenv("WW_NTFY_TOPIC", "ww-test")
    monkeypatch.setenv("WW_NTFY_SERVER", "https://ntfy.example.test")
    _samples(conn, monkeypatch, tmp_path, mem=3_300_000_000)
    pushed = []

    def handler(request):
        pushed.append((request.headers["Title"], request.content.decode()))
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await usage.check(conn, client, NOW)
        await usage.check(conn, client, NOW + 600)  # same warning: recorded, not pushed again
        await usage.check(conn, client, NOW + DAY + 60)
    assert len(pushed) == 2 and pushed[0][0] == "Worldwatch resources"
    assert "memory at 87%" in pushed[0][1]
    n = conn.execute("SELECT COUNT(*) FROM health WHERE component='resources' AND event='warning'").fetchone()[0]
    assert n == 3


def test_api_resources(tmp_path, monkeypatch):
    path = tmp_path / "u.db"
    conn = open_db(path)
    _samples(conn, monkeypatch, tmp_path)
    conn.execute("INSERT INTO usage VALUES ('goes19_fire', ?, 1000000, 0, 5)", (NOW // DAY * DAY,))
    conn.commit()
    client = TestClient(create_app(path, now_fn=lambda: NOW))
    body = client.get("/api/resources").json()
    assert body["traffic"]["today"]["sources"][0]["component"] == "goes19_fire"
    page = client.get("/resources")
    assert page.status_code == 200 and "Downloads by source" in page.text


def test_a_restart_burst_is_not_a_bandwidth_warning(tmp_path, monkeypatch):
    """Two hours of samples at 200 MB an hour extrapolate to 4.8 GB a day; too
    short a span to judge (a restart re-fetching what it lost), so no warning."""
    conn = open_db(tmp_path / "u.db")
    for h in range(3):
        _fake_slice(tmp_path, monkeypatch, 1_000_000_000, 1_000_000_000, 3_774_873_600, h,
                    (h * 200_000_000, 0))
        usage.sample(conn, NOW - (2 - h) * 3600, tmp_path / "u.db")
    rep = usage.report(conn, NOW)
    assert rep["net_in_mb_day"] == 4800.0 and rep["net_in_hours"] == 2.0
    assert rep["warnings"] == []
