"""Active prober (ADR 0002 §E): stratified targets, physics and vantage checks,
failures as a per-country count — with fake DNS and fake probes."""

import asyncio

import httpx

from worldwatch.config.countries import country_cell
from worldwatch.layer0.live import LiveScorer
from worldwatch.probe.prober import (
    Target,
    _spread,
    discover_ntp,
    min_rtt_ms,
    round_observations,
    run_prober,
)


def _zones(mapping):
    async def resolve(host):
        return mapping.get(host, [])
    return resolve


async def test_ntp_discovery_drops_continent_fallback_and_spreads_networks():
    resolve = _zones({
        "0.de.pool.ntp.org": ["1.2.3.4", "1.2.3.5", "1.2.9.9", "5.6.7.8"],
        "1.de.pool.ntp.org": ["1.2.4.4", "9.9.9.9"],
        "0.ke.pool.ntp.org": ["9.9.9.9", "41.0.0.1"],  # 9.9.9.9 also under DE: a fallback
    })
    ts = await discover_ntp(["DE", "KE"], quota=8, resolve=resolve)
    de = sorted(t.ip for t in ts if t.cc == "DE")
    assert "9.9.9.9" not in de and "9.9.9.9" not in {t.ip for t in ts if t.cc == "KE"}
    assert de == ["1.2.3.4", "1.2.4.4", "5.6.7.8"]  # 1.2.3.5 shares a /24; 1.2.9.9 a full /16
    assert [t.ip for t in ts if t.cc == "KE"] == ["41.0.0.1"]


def test_spread_respects_quota():
    ips = [f"10.{i}.0.1" for i in range(20)]
    assert len(_spread(ips, 8)) == 8


def test_light_speed_bound_grows_with_distance():
    v = (49.0, 8.4)
    assert min_rtt_ms(v, "DE") < 5 < min_rtt_ms(v, "US") < min_rtt_ms(v, "AU")
    assert 70 < min_rtt_ms(v, "AU") < 90  # ~16,000 km → ≥ 80 ms there-and-back, halved


def test_round_emits_failures_only_as_country_counts(sources):
    cfg = sources["probe_reachability"]
    targets = [Target(f"10.0.0.{i}", "NG", "ntp") for i in range(6)] + [Target("10.1.0.1", "DE", "ntp")]
    results = {t.ip: (None if t.ip.endswith((".0", ".1", ".2")) else 120.0) for t in targets}
    results["10.1.0.1"] = 8.0
    obs, tally = round_observations(cfg, targets, results, 1000, 3)
    assert tally == {"NG": (3, 6), "DE": (0, 1)}
    assert len(obs) == 3 and {o.cell for o in obs} == {country_cell("NG", 3)}
    assert len({o.ts for o in obs}) == 3
    assert obs[0].context == {"country": "Nigeria", "cc": "NG", "failed": 3, "probed": 6, "_rank": 3}
    assert all("10." not in str(o.context) for o in obs)  # no target IPs leave the prober


class _NoAnchors(httpx.AsyncClient):
    def __init__(self):
        super().__init__(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"results": []})))


async def _one_round(db, cfg, resolve, ntp, live=None, rounds=1):
    stop = asyncio.Event()
    got = []

    async def on_new(c, obs, t):
        got.extend(obs)
        if live:
            live.ingest(c, obs, t)

    calls = {"n": 0}

    async def counted_ntp(ips):
        calls["n"] += 1
        r = await ntp(ips, calls["n"])
        if calls["n"] >= rounds:
            stop.set()
        return r

    async def no_tcp(ips):
        return {}

    fast = type(cfg)(**{**cfg.__dict__, "fetch": {**cfg.fetch, "round_seconds": 0}})
    async with _NoAnchors() as client:
        await asyncio.wait_for(run_prober(db, fast, client, on_new=on_new, stop=stop, resolve=resolve,
                                          ntp=counted_ntp, tcp=no_tcp,
                                          register_cells=live.register_cells if live else None), 10)
    return got


async def test_country_outage_becomes_failures_and_zeros_are_trained(db, sources):
    cfg = sources["probe_reachability"]
    zones = {f"{n}.ng.pool.ntp.org": [f"41.{n}.{i}.1" for i in range(3)] for n in range(4)}
    for cc, first in (("de", 5), ("fr", 6), ("jp", 7)):  # enough elsewhere: Nigeria is a minority
        zones[f"0.{cc}.pool.ntp.org"] = [f"{first}.{i}.0.1" for i in range(8)]
    resolve = _zones(zones)

    async def ntp(ips, round_no):  # round 1: all answer; round 2: Nigeria goes dark
        return {ip: (None if round_no == 2 and ip.startswith("41.") else 90.0) for ip in ips}

    live = LiveScorer(db, sources)
    got = await _one_round(db, cfg, resolve, ntp, live=live, rounds=2)
    assert len(got) == 8 and {o.cell for o in got} == {country_cell("NG", 3)}
    cells = {r[0] for r in db.execute("SELECT cell FROM live_cells WHERE stream_id = 'probe_reachability'")}
    assert cells == {country_cell(c, 3) for c in ("NG", "DE", "FR", "JP")}  # registered up front


async def test_everything_failing_is_our_own_network(db, sources):
    cfg = sources["probe_reachability"]
    resolve = _zones({"0.de.pool.ntp.org": ["5.1.0.1", "5.2.0.1"], "0.fr.pool.ntp.org": ["6.1.0.1", "6.2.0.1"]})

    async def ntp(ips, round_no):
        return {ip: (90.0 if round_no == 1 else None) for ip in ips}

    got = await _one_round(db, cfg, resolve, ntp, rounds=2)
    assert got == []
    assert db.execute("SELECT COUNT(*) FROM health WHERE event = 'vantage_fault'").fetchone()[0] == 1


async def test_too_fast_reply_rejects_a_mislocated_target(db, sources):
    cfg = sources["probe_reachability"]
    resolve = _zones({"0.au.pool.ntp.org": ["1.1.0.1", "1.2.0.1"]})

    async def ntp(ips, round_no):
        return {"1.1.0.1": 3.0, "1.2.0.1": 290.0}  # 3 ms to "Australia" from Germany: impossible

    await _one_round(db, cfg, resolve, ntp)
    rej = db.execute("SELECT ip, rejected FROM probe_targets WHERE rejected IS NOT NULL").fetchall()
    assert [r[0] for r in rej] == ["1.1.0.1"]


async def test_firewalled_targets_are_dropped_at_discovery(db, sources):
    cfg = sources["probe_reachability"]
    resolve = _zones({"0.de.pool.ntp.org": ["5.1.0.1", "5.2.0.1", "5.3.0.1"]})

    async def ntp(ips, round_no):  # 5.3.0.1 never answers; round 2 must not count it
        return {ip: (None if ip == "5.3.0.1" else 9.0) for ip in ips}

    got = await _one_round(db, cfg, resolve, ntp, rounds=2)
    assert got == []


async def test_opted_out_hosts_are_never_probed(db, sources):
    import dataclasses

    cfg = sources["probe_reachability"]
    cfg = dataclasses.replace(cfg, fetch={**cfg.fetch, "exclude": ["5.2.0.0/16", "5.3.0.1"]})
    resolve = _zones({"0.de.pool.ntp.org": ["5.1.0.1", "5.2.0.1", "5.3.0.1", "5.4.0.1"]})
    probed: list[str] = []

    async def ntp(ips, round_no):
        probed.extend(ips)
        return {ip: 9.0 for ip in ips}

    await _one_round(db, cfg, resolve, ntp)
    assert sorted(set(probed)) == ["5.1.0.1", "5.4.0.1"]
