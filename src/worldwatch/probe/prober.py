"""Active prober (ADR 0002 §E): our own fast, bias-controlled reachability tripwire.

Every `round_seconds`, probe a stratified set of public targets per country
and turn failures into a count stream per country, scored live like any other
source. Runs inside the poll process as `[fetch] kind = "probe"`.

Targets (refreshed every `refresh_seconds`):
  - NTP pool country zones: `0–3.<cc>.pool.ntp.org`, queried with NTP (UDP 123,
    unprivileged; ≥ 64 s per server, as the pool asks). An address returned
    for more than one country is the pool's continent fallback and is dropped.
  - RIPE Atlas anchors (hosts meant to be measured), probed by TCP connect.
  - A fixed quota per country, spread across networks (≤ 1 per /24, ≤ 2 per
    /16 for NTP; distinct ASNs for anchors). Sampling in proportion to
    address space would reproduce the US bias; a quota removes it.

Checks:
  - Location by physics: a reply faster than light-in-fibre allows for the
    country's distance from our vantage rejects the target.
  - Our own network: if more than `vantage_fault_fraction` of all targets fail
    in one round, the round is recorded as `vantage_fault` and nothing is
    emitted (the spec's poller/network-fault attribution).

Privacy: target IPs stay in `probe_targets`; the evidence store, API and
pushes only ever see country-level counts.
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import socket
import sqlite3
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from worldwatch.config.countries import countries, country_cell
from worldwatch.config.loader import SourceConfig
from worldwatch.ingest.models import Observation
from worldwatch.instrument import record_health
from worldwatch.poll.http import USER_AGENT
from worldwatch.store import write_new_observations

OnNew = Callable[[SourceConfig, list[Observation], int], Awaitable[None]]
NTP_REQUEST = b"\x23" + b"\x00" * 47  # LI 0, version 4, mode 3 (client)
FIBRE_KM_PER_MS = 200.0  # light in fibre ≈ 2/3 c
RIPE_ANCHORS = "https://atlas.ripe.net/api/v2/anchors/?page_size=500"


@dataclass(frozen=True)
class Target:
    ip: str
    cc: str
    kind: str  # "ntp" | "anchor"
    asn: int | None = None


# --- discovery -----------------------------------------------------------------------


async def discover_ntp(
    ccs: list[str], quota: int, resolve: Callable[[str], Awaitable[list[str]]]
) -> list[Target]:
    """NTP pool country-zone servers, a quota per country across networks."""
    sem = asyncio.Semaphore(20)
    found: dict[str, set[str]] = defaultdict(set)  # ip → countries it was listed for

    async def zone(cc: str, n: int) -> None:
        async with sem:
            try:
                for ip in await resolve(f"{n}.{cc.lower()}.pool.ntp.org"):
                    found[ip].add(cc)
            except Exception:
                pass  # an empty or failing zone is simply unmonitored

    await asyncio.gather(*(zone(cc, n) for cc in ccs for n in range(4)))
    by_cc: dict[str, list[str]] = defaultdict(list)
    for ip, ccs_of_ip in found.items():
        if len(ccs_of_ip) == 1:  # listed for several countries = continent fallback
            by_cc[next(iter(ccs_of_ip))].append(ip)
    out: list[Target] = []
    for cc, ips in sorted(by_cc.items()):
        out += [Target(ip, cc, "ntp") for ip in _spread(sorted(ips), quota)]
    return out


def _spread(ips: list[str], quota: int) -> list[str]:
    """At most one address per /24 and two per /16 — different networks."""
    seen24: set[str] = set()
    per16: dict[str, int] = defaultdict(int)
    out = []
    for ip in ips:
        try:
            a = ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        n24, n16 = str(ipaddress.IPv4Network(f"{a}/24", strict=False)), ip.rsplit(".", 2)[0]
        if n24 in seen24 or per16[n16] >= 2:
            continue
        seen24.add(n24)
        per16[n16] += 1
        out.append(ip)
        if len(out) >= quota:
            break
    return out


async def discover_anchors(client: httpx.AsyncClient, quota: int) -> list[Target]:
    """RIPE Atlas anchors with IPv4, a quota per country across distinct ASNs."""
    url: str | None = RIPE_ANCHORS
    rows: list[dict[str, Any]] = []
    while url:
        resp = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
        resp.raise_for_status()
        page = resp.json()
        rows += page.get("results") or []
        url = page.get("next")
    by_cc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in rows:
        if a.get("ip_v4") and a.get("country") and not a.get("is_disabled"):
            by_cc[str(a["country"]).upper()].append(a)
    out: list[Target] = []
    for cc, anchors in sorted(by_cc.items()):
        asns: set[int] = set()
        for a in sorted(anchors, key=lambda a: a.get("id") or 0):
            asn = a.get("as_v4")
            if asn in asns:
                continue
            asns.add(asn)
            out.append(Target(a["ip_v4"], cc, "anchor", asn))
            if len(asns) >= quota:
                break
    return out


async def system_resolve(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, 123, family=socket.AF_INET, type=socket.SOCK_DGRAM)
    return sorted({i[4][0] for i in infos})


# --- probing ---------------------------------------------------------------------------


class _NTPProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.replies: dict[str, float] = {}

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) >= 48 and (data[0] & 0x07) == 4:  # mode 4 = server reply
            self.replies.setdefault(addr[0], time.monotonic())


async def probe_ntp(ips: list[str], timeout: float = 3.0, spread: float = 10.0) -> dict[str, float | None]:
    """RTT in ms per address (None = no reply), over one shared UDP socket;
    sends are spread over `spread` seconds so the round is not a burst."""
    if not ips:
        return {}
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(_NTPProtocol, family=socket.AF_INET)
    sent: dict[str, float] = {}
    try:
        gap = spread / max(len(ips), 1)
        for ip in ips:
            sent[ip] = time.monotonic()
            transport.sendto(NTP_REQUEST, (ip, 123))
            await asyncio.sleep(gap)
        await asyncio.sleep(timeout)
    finally:
        transport.close()
    return {ip: ((proto.replies[ip] - t0) * 1000 if ip in proto.replies else None)
            for ip, t0 in sent.items()}


async def probe_tcp(ips: list[str], port: int = 80, timeout: float = 3.0) -> dict[str, float | None]:
    sem = asyncio.Semaphore(100)

    async def one(ip: str) -> tuple[str, float | None]:
        async with sem:
            t0 = time.monotonic()
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
                writer.close()
            except ConnectionRefusedError:
                pass  # a reset still proves the host is reachable
            except Exception:
                return ip, None
            return ip, (time.monotonic() - t0) * 1000

    return dict(await asyncio.gather(*(one(ip) for ip in ips)))


# --- physics & aggregation -----------------------------------------------------------


def min_rtt_ms(vantage: tuple[float, float], cc: str) -> float:
    """Lower bound on round-trip time to a country's representative point:
    great-circle distance there and back at light-in-fibre speed, halved to
    allow for the country's extent."""
    c = countries().get(cc)
    if c is None:
        return 0.0
    lat1, lon1, lat2, lon2 = map(math.radians, (vantage[0], vantage[1], c[1], c[2]))
    d = 2 * 6371 * math.asin(math.sqrt(
        math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    ))
    return 0.5 * (2 * d / FIBRE_KM_PER_MS)


def round_observations(
    cfg: SourceConfig,
    targets: list[Target],
    results: dict[str, float | None],
    round_ts: int,
    resolution: int,
) -> tuple[list[Observation], dict[str, tuple[int, int]]]:
    """Failures → one pure-event observation each, in its country's cell."""
    per_cc: dict[str, list[Target]] = defaultdict(list)
    for t in targets:
        if t.ip in results:
            per_cc[t.cc].append(t)
    obs: list[Observation] = []
    tally: dict[str, tuple[int, int]] = {}
    for cc, ts_ in sorted(per_cc.items()):
        failed = [t for t in ts_ if results.get(t.ip) is None]
        tally[cc] = (len(failed), len(ts_))
        cell = country_cell(cc, resolution)
        if cell is None:
            continue
        for i, _ in enumerate(failed):
            obs.append(Observation(cfg.stream_id, cell, round_ts + i, None, context={
                "country": countries()[cc][0], "cc": cc,
                "failed": len(failed), "probed": len(ts_), "_rank": len(failed),
            }))
    return obs, tally


# --- the loop ----------------------------------------------------------------------------


async def run_prober(
    conn: sqlite3.Connection,
    cfg: SourceConfig,
    client: httpx.AsyncClient,
    *,
    on_new: OnNew | None = None,
    register_cells: Callable[[str, list[str], int], None] | None = None,
    stop: asyncio.Event | None = None,
    resolve: Callable[[str], Awaitable[list[str]]] = system_resolve,
    ntp: Callable[[list[str]], Awaitable[dict[str, float | None]]] = probe_ntp,
    tcp: Callable[[list[str]], Awaitable[dict[str, float | None]]] = probe_tcp,
    clock: Callable[[], float] = time.time,
) -> None:
    f = cfg.fetch
    round_seconds = int(f.get("round_seconds", 120))
    refresh = int(f.get("refresh_seconds", 86400))
    res = int(cfg.geocode.get("h3_resolution", 2))
    vantage = (float(f.get("vantage_lat", 49.45)), float(f.get("vantage_lon", 11.08)))
    fault_frac = float(f.get("vantage_fault_fraction", 0.5))
    targets: list[Target] = []
    refreshed = 0.0
    while stop is None or not stop.is_set():
        started = clock()
        try:
            fresh = not targets or started - refreshed >= refresh
            if fresh:
                targets = await _discover(conn, cfg, client, resolve, int(started))
                refreshed = started
            results = {
                **await ntp([t.ip for t in targets if t.kind == "ntp"]),
                **await tcp([t.ip for t in targets if t.kind == "anchor"]),
            }
            if fresh:
                # qualify: keep targets that answer now (a firewalled one would add a
                # failure every round). Only until the next refresh — never a blacklist.
                answering = {ip for ip, r in results.items() if r is not None}
                if len(answering) > (1 - fault_frac) * len(results):
                    targets = [t for t in targets if t.ip in answering]
                    results = {ip: r for ip, r in results.items() if ip in answering}
                if register_cells:
                    cells = sorted({c for t in targets if (c := country_cell(t.cc, res))})
                    register_cells(cfg.stream_id, cells, int(started))
            # physics: a reply faster than light allows means the target isn't where it claims
            too_fast = [t for t in targets if (r := results.get(t.ip)) is not None
                        and r < min_rtt_ms(vantage, t.cc)]
            if too_fast:
                _reject(conn, too_fast, "rtt below light-in-fibre bound")
                bad = {t.ip for t in too_fast}
                targets = [t for t in targets if t.ip not in bad]
                results = {ip: r for ip, r in results.items() if ip not in bad}
            round_ts = int(started)
            failed = sum(r is None for r in results.values())
            if results and failed / len(results) > fault_frac:
                record_health(conn, cfg.stream_id, "vantage_fault",
                              f"failed={failed}/{len(results)}", ts=round_ts)
            else:
                obs, tally = round_observations(cfg, targets, results, round_ts, res)
                new = write_new_observations(conn, obs, now=round_ts)
                record_health(conn, cfg.stream_id, "ok",
                              f"probed={len(results)} failed={failed} countries={len(tally)}",
                              ts=round_ts)
                if on_new is not None and new:
                    await on_new(cfg, new, round_ts)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # isolation boundary: a prober fault is data, never a crash
            record_health(conn, cfg.stream_id, "probe_error", f"{type(e).__name__}: {e}"[:300],
                          ts=int(clock()))
        await asyncio.sleep(max(1.0, round_seconds - (clock() - started)))


async def _discover(
    conn: sqlite3.Connection,
    cfg: SourceConfig,
    client: httpx.AsyncClient,
    resolve: Callable[[str], Awaitable[list[str]]],
    now: int,
) -> list[Target]:
    f = cfg.fetch
    rejected = {r[0] for r in conn.execute("SELECT ip FROM probe_targets WHERE rejected IS NOT NULL")}
    ntp_targets = await discover_ntp(sorted(countries()), int(f.get("ntp_quota", 8)), resolve)
    try:
        anchors = await discover_anchors(client, int(f.get("anchor_quota", 4)))
    except Exception as e:
        record_health(conn, cfg.stream_id, "probe_error", f"anchors: {type(e).__name__}: {e}"[:300], ts=now)
        anchors = []
    targets = [t for t in ntp_targets + anchors if t.ip not in rejected]
    conn.execute("DELETE FROM probe_targets WHERE rejected IS NULL")
    conn.executemany(
        "INSERT OR IGNORE INTO probe_targets (ip, cc, kind, asn, discovered_at) VALUES (?, ?, ?, ?, ?)",
        [(t.ip, t.cc, t.kind, t.asn, now) for t in targets],
    )
    conn.commit()
    record_health(conn, cfg.stream_id, "discovered",
                  f"targets={len(targets)} countries={len({t.cc for t in targets})}", ts=now)
    return targets


def _reject(conn: sqlite3.Connection, targets: list[Target], why: str) -> None:
    conn.executemany("UPDATE probe_targets SET rejected = ? WHERE ip = ?", [(why, t.ip) for t in targets])
    conn.commit()
