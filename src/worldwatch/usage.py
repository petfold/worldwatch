"""Resource use as data: bytes per source, and the slice's memory, CPU, disk, net.

Two records, both small:
  usage      bytes in/out and requests per (component, UTC day). Every request
             of the poll process goes through CountingTransport, which charges
             it to the asyncio task's component (a stream id; "live" for the
             alert ticker; "other" otherwise). Bytes are as on the wire
             (compressed bodies, approximate headers; no TCP/TLS overhead).
  resources  a sample every SAMPLE_SECONDS: the worldwatch.slice cgroup's memory
             (current, peak, cap) and CPU time, its network bytes (systemd
             IPAccounting=yes on the slice, so they include the overhead and
             every worldwatch process), the host's interfaces, free disk and
             the database size. Counters are cumulative; report() takes deltas.

report() says what a source costs and warns when one is disproportionate (a
large share of the day's downloads, or above a per-source cap) or a resource
nears its limit. check() records each warning as health data and pushes it,
at most once a day per kind. Never read by detection (guardrail 8 spirit:
this is about the machine, not the world).
"""

from __future__ import annotations

import contextvars
import os
import sqlite3
import subprocess
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

SAMPLE_SECONDS = 600
RETENTION_DAYS = 90
DAY = 86400

_component: contextvars.ContextVar[str] = contextvars.ContextVar("ww_component", default="other")
_counts: dict[str, list[int]] = {}  # component -> [bytes_in, bytes_out, requests], unflushed


def tag(component: str) -> None:
    """Charge this asyncio task's requests (and its children's) to `component`."""
    _component.set(component)


def add(component: str, bytes_in: int = 0, bytes_out: int = 0, requests: int = 0) -> None:
    """Charge traffic that doesn't go through the HTTP client (websocket messages)."""
    acc = _counts.setdefault(component, [0, 0, 0])
    acc[0] += bytes_in
    acc[1] += bytes_out
    acc[2] += requests


def _headers_size(headers: Any) -> int:
    return sum(len(k) + len(v) + 4 for k, v in headers.raw)


def _request_size(request: httpx.Request) -> int:
    size = len(request.method) + len(request.url.raw_path) + 12 + _headers_size(request.headers)
    try:
        size += len(request.content)
    except httpx.RequestNotRead:  # a streamed upload: its length if declared
        size += int(request.headers.get("content-length", 0) or 0)
    return size


class _CountingStream(httpx.AsyncByteStream):
    def __init__(self, inner: Any, acc: list[int]) -> None:
        self._inner = inner
        self._acc = acc

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            self._acc[0] += len(chunk)
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class CountingTransport(httpx.AsyncBaseTransport):
    """An HTTP transport that counts each request's bytes for its component.
    The response body is counted as it streams through, before decompression."""

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        acc = _counts.setdefault(_component.get(), [0, 0, 0])
        acc[1] += _request_size(request)
        acc[2] += 1
        response = await self._inner.handle_async_request(request)
        acc[0] += 17 + _headers_size(response.headers)
        if isinstance(response.stream, httpx.ByteStream):  # a body already in memory
            acc[0] += sum(len(chunk) for chunk in response.stream)
        else:
            response.stream = _CountingStream(response.stream, acc)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def flush(conn: sqlite3.Connection, now: int) -> int:
    """Add the unflushed counts to today's usage rows; returns components written."""
    day = now // DAY * DAY
    items = [(c, v) for c, v in _counts.items() if any(v)]
    for component, (b_in, b_out, reqs) in items:
        conn.execute(
            "INSERT INTO usage (component, day, bytes_in, bytes_out, requests) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (component, day) DO UPDATE SET bytes_in = bytes_in + excluded.bytes_in, "
            "bytes_out = bytes_out + excluded.bytes_out, requests = requests + excluded.requests",
            (component, day, b_in, b_out, reqs),
        )
    conn.commit()
    _counts.clear()
    return len(items)


# --- sampling -----------------------------------------------------------------


def _slice_dir(slice_name: str) -> Path | None:
    """This process's ancestor cgroup named `slice_name` (cgroup v2), if any."""
    try:
        rel = Path("/proc/self/cgroup").read_text().split("\n")[0].split("::", 1)[1].strip()
    except (OSError, IndexError):
        return None
    parts = Path(rel).parts
    for i in range(len(parts), 0, -1):
        if parts[i - 1] == slice_name:
            return Path("/sys/fs/cgroup", *parts[1:i])
    return None


def _read_int(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return None if text == "max" else int(text)


def _cpu_usec(cg: Path) -> int | None:
    try:
        for line in (cg / "cpu.stat").read_text().splitlines():
            if line.startswith("usage_usec "):
                return int(line.split()[1])
    except OSError:
        pass
    return None


def _ip_accounting(slice_name: str) -> tuple[int | None, int | None]:
    try:
        out = subprocess.run(
            ["systemctl", "show", slice_name, "-p", "IPIngressBytes", "-p", "IPEgressBytes"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    vals = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def num(key: str) -> int | None:
        v = vals.get(key, "")
        return int(v) if v.isdigit() else None  # "[no data]" while accounting is off

    return num("IPIngressBytes"), num("IPEgressBytes")


def _host_net() -> tuple[int | None, int | None]:
    try:
        lines = Path("/proc/net/dev").read_text().splitlines()[2:]
    except OSError:
        return None, None
    rx = tx = 0
    for line in lines:
        name, data = line.split(":", 1)
        if name.strip() == "lo":
            continue
        f = data.split()
        rx += int(f[0])
        tx += int(f[8])
    return rx, tx


def sample(conn: sqlite3.Connection, now: int, db_file: Path,
           slice_name: str = "worldwatch.slice") -> dict[str, Any]:
    """Take one resources sample (what isn't readable here is NULL) and prune old ones."""
    cg = _slice_dir(slice_name)
    net_in, net_out = _ip_accounting(slice_name) if cg else (None, None)
    host_in, host_out = _host_net()
    try:
        st = os.statvfs(db_file.parent)
        disk_free, disk_total = st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
    except OSError:
        disk_free = disk_total = None
    db_bytes = sum(p.stat().st_size for p in (db_file, Path(f"{db_file}-wal")) if p.exists())
    row = {
        "ts": now,
        "mem_current": _read_int(cg / "memory.current") if cg else None,
        "mem_peak": _read_int(cg / "memory.peak") if cg else None,
        "mem_max": _read_int(cg / "memory.max") if cg else None,
        "cpu_usec": _cpu_usec(cg) if cg else None,
        "net_in": net_in, "net_out": net_out, "host_in": host_in, "host_out": host_out,
        "disk_free": disk_free, "disk_total": disk_total, "db_bytes": db_bytes,
    }
    conn.execute(
        f"INSERT OR REPLACE INTO resources ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
        tuple(row.values()),
    )
    conn.execute("DELETE FROM resources WHERE ts < ?", (now - RETENTION_DAYS * DAY,))
    conn.execute("DELETE FROM usage WHERE day < ?", (now - RETENTION_DAYS * DAY,))
    conn.commit()
    return row


# --- the report and its warnings ------------------------------------------------


def _limits() -> dict[str, float]:
    env = os.environ.get
    return {
        "budget_mb_day": float(env("WW_BANDWIDTH_BUDGET_MB", "3000")),
        "source_share": float(env("WW_SOURCE_SHARE_MAX", "0.25")),
        "source_mb_day": float(env("WW_SOURCE_DAY_MAX_MB", "1000")),
        "share_floor_mb": 100.0,  # below this a big share of a small day is no problem
        "mem_frac": float(env("WW_MEM_WARN_FRAC", "0.85")),
        "disk_min_gb": float(env("WW_DISK_MIN_FREE_GB", "10")),
    }


def _delta(new: int | None, old: int | None) -> int | None:
    if new is None or old is None:
        return None
    return new - old if new >= old else new  # a counter that restarted (reboot)


WARN_SPAN = 6 * 3600  # a rate is judged only over this much: a restart's burst is no day


def _per_day(conn: sqlite3.Connection, now: int, col: str) -> tuple[float, int] | None:
    """Bytes a day of a cumulative counter from samples over the last 24 h, and
    the seconds the samples span."""
    rows = conn.execute(
        f"SELECT ts, {col} AS v FROM resources WHERE ts >= ? AND {col} IS NOT NULL ORDER BY ts",
        (now - DAY,)).fetchall()
    if len(rows) < 2 or rows[-1]["ts"] - rows[0]["ts"] < 3600:
        return None
    span = rows[-1]["ts"] - rows[0]["ts"]
    total = sum(_delta(b["v"], a["v"]) or 0 for a, b in zip(rows, rows[1:], strict=False))
    return total * DAY / span, span


def report(conn: sqlite3.Connection, now: int | None = None) -> dict[str, Any]:
    """Where the machine stands: the last sample, rates over the last day, the
    sources ranked by download for the last full UTC day and today, warnings."""
    now = int(time.time()) if now is None else now
    lim = _limits()
    last = conn.execute("SELECT * FROM resources ORDER BY ts DESC LIMIT 1").fetchone()
    hour = conn.execute("SELECT * FROM resources WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
                        (now - 3600,)).fetchone()
    out: dict[str, Any] = {"ts": now, "sample": dict(last) if last else None, "limits": lim}

    if last and hour and last["cpu_usec"] is not None and hour["cpu_usec"] is not None:
        out["cpu_percent_of_one_core"] = round(
            100 * (_delta(last["cpu_usec"], hour["cpu_usec"]) or 0) / 1e6 / max(1, last["ts"] - hour["ts"]), 1)
    if last and last["mem_current"] and last["mem_max"]:
        out["memory_fraction"] = round(last["mem_current"] / last["mem_max"], 3)
        out["memory_peak_fraction"] = round((last["mem_peak"] or 0) / last["mem_max"], 3)
    for col in ("net_in", "net_out", "host_in", "host_out"):
        rate = _per_day(conn, now, col)
        out[f"{col}_mb_day"] = None if rate is None else round(rate[0] / 1e6, 1)
        out[f"{col}_hours"] = None if rate is None else round(rate[1] / 3600, 1)

    today = now // DAY * DAY
    days = {}
    for label, day in (("yesterday", today - DAY), ("today", today)):
        rows = conn.execute(
            "SELECT component, bytes_in, bytes_out, requests FROM usage WHERE day = ? "
            "ORDER BY bytes_in DESC", (day,)).fetchall()
        total = sum(r["bytes_in"] for r in rows)
        days[label] = {
            "total_mb_in": round(total / 1e6, 1),
            "total_mb_out": round(sum(r["bytes_out"] for r in rows) / 1e6, 1),
            "sources": [{"component": r["component"], "mb_in": round(r["bytes_in"] / 1e6, 2),
                         "mb_out": round(r["bytes_out"] / 1e6, 2), "requests": r["requests"],
                         "share": round(r["bytes_in"] / total, 3) if total else 0.0}
                        for r in rows],
        }
    out["traffic"] = days
    out["warnings"] = _warnings(out, lim, now)
    return out


def _warnings(rep: dict[str, Any], lim: dict[str, float], now: int) -> list[dict[str, str]]:
    warn: list[dict[str, str]] = []
    s = rep.get("sample") or {}
    if rep.get("memory_fraction", 0) >= lim["mem_frac"]:
        warn.append({"kind": "memory", "text": f"memory at {rep['memory_fraction']:.0%} of the "
                     f"{s['mem_max'] / 2**20:.0f} MiB cap"})
    if s.get("disk_free") is not None and s["disk_free"] < lim["disk_min_gb"] * 1e9:
        warn.append({"kind": "disk", "text": f"only {s['disk_free'] / 1e9:.1f} GB disk free"})
    net = rep.get("net_in_mb_day")
    if net is not None and (rep.get("net_in_hours") or 0) * 3600 >= WARN_SPAN and net > lim["budget_mb_day"]:
        warn.append({"kind": "bandwidth", "text": f"downloading {net:.0f} MB/day, budget "
                     f"{lim['budget_mb_day']:.0f}"})
    # a source's cost: a full day's, or today's so far scaled to a day once 6 h are in
    day_frac = (now % DAY) / DAY
    for label, scale in (("yesterday", 1.0), ("today", 1 / day_frac if day_frac >= 0.25 else 0.0)):
        for src in rep["traffic"][label]["sources"]:
            mb_day = src["mb_in"] * scale
            if mb_day > lim["source_mb_day"] or (
                    src["share"] > lim["source_share"] and mb_day > lim["share_floor_mb"]):
                kind = f"source:{src['component']}"
                if all(w["kind"] != kind for w in warn):
                    warn.append({"kind": kind, "text": f"{src['component']} downloads "
                                 f"~{mb_day:.0f} MB/day, {src['share']:.0%} of all ({label})"})
    return warn


async def check(conn: sqlite3.Connection, client: httpx.AsyncClient, now: int) -> list[dict[str, str]]:
    """Record each warning as health data; push the ones not pushed in the last day."""
    from worldwatch.api.notify import NtfyConfig, send_ntfy_note
    from worldwatch.instrument import record_health

    warnings = report(conn, now)["warnings"]
    cfg = NtfyConfig.from_env()
    for w in warnings:
        record_health(conn, "resources", "warning", f"{w['kind']}: {w['text']}", ts=now)
        pushed = conn.execute(
            "SELECT 1 FROM health WHERE component = 'resources' AND event = 'pushed' "
            "AND ts > ? AND detail = ?", (now - DAY, w["kind"])).fetchone()
        if pushed is None and cfg is not None:
            try:
                await send_ntfy_note(client, cfg, "Worldwatch resources", w["text"], priority=3)
                record_health(conn, "resources", "pushed", w["kind"], ts=now)
            except httpx.HTTPError as e:
                record_health(conn, "resources", "push_error", str(e)[:300], ts=now)
    return warnings


def summary_lines(rep: dict[str, Any], top: int = 10) -> list[str]:
    """The report as the markdown the digest renders (headings, bullets, bold)."""

    def mb(v: float | None) -> str:
        return "n/a" if v is None else f"{v:,.0f} MB"

    s = rep.get("sample") or {}
    out = ["## Resources", ""]
    if rep["warnings"]:
        out += [f"- **{w['text']}**" for w in rep["warnings"]]
    else:
        out.append("- No resource warnings.")
    if s.get("mem_current") is not None:
        cap = f" of {s['mem_max'] / 2**20:,.0f} MiB" if s.get("mem_max") else ""
        peak = f", peak {s['mem_peak'] / 2**20:,.0f} MiB" if s.get("mem_peak") else ""
        out.append(f"- Memory: {s['mem_current'] / 2**20:,.0f} MiB{cap}{peak}")
    if rep.get("cpu_percent_of_one_core") is not None:
        out.append(f"- CPU, last hour: {rep['cpu_percent_of_one_core']}% of one core")
    if s.get("disk_free") is not None:
        out.append(f"- Disk: {s['disk_free'] / 1e9:,.1f} GB free; database {s['db_bytes'] / 1e9:,.2f} GB")
    hours = f" (from {rep['net_in_hours']} h of samples)" if rep.get("net_in_hours") and rep["net_in_hours"] < 24 else ""
    out.append(f"- Network, Worldwatch (last 24 h): {mb(rep.get('net_in_mb_day'))} in, "
               f"{mb(rep.get('net_out_mb_day'))} out a day{hours}")
    out.append(f"- Network, whole host (last 24 h): {mb(rep.get('host_in_mb_day'))} in, "
               f"{mb(rep.get('host_out_mb_day'))} out a day")
    for label in ("yesterday", "today"):
        day = rep["traffic"][label]
        out += ["", f"### Downloads by source, {label} (UTC): {day['total_mb_in']:,.1f} MB in, "
                f"{day['total_mb_out']:,.1f} MB out", ""]
        out += [f"- {src['component']}: {src['mb_in']:,.1f} MB ({src['share']:.0%}), "
                f"{src['requests']} requests" for src in day["sources"][:top]]
        if len(day["sources"]) > top:
            rest = sum(x["mb_in"] for x in day["sources"][top:])
            out.append(f"- {len(day['sources']) - top} others: {rest:,.1f} MB")
    out.append("")
    return out
