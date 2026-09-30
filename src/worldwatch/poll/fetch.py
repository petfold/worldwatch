"""Per-source fetchers: how a source's endpoint is turned into a parser payload.

The default fetch is one conditional JSON GET (`json_get`) — that covers every
plain feed. A genuinely different fetch *shape* (multi-step, binary, special
auth dance) registers a new fetcher here and is selected by the stanza's
[<source>.fetch] `kind` key, so sources stay config-driven (guardrail 2).

Fetchers raise httpx errors for the poller to classify (timeout / http_error);
any other exception is a fetch fault the poller records as `fetch_error`.

kinds:
  json_get           (default) conditional GET; if the stanza names an
                     `auth_env_var`, its value is sent as a Bearer token
                     (`auth_scheme` changes the word: Open Exchange Rates
                     wants "Token"); `auth_scheme = "url"`: no header, the key
                     goes in the endpoint's `{auth}` (EIA accepts it nowhere else)
  earthdata_granule  NASA Earthdata: GET the endpoint (a CMR granule search,
                     newest first) → newest granule id + download URL; skip if
                     the id matches the last fetched one (stored in the ETag
                     validator slot); else download the granule bytes with an
                     Earthdata Login (EDL) bearer token. The token is reused
                     from `token_env` if set, else listed/minted via the URS
                     API from `user_env`/`pass_env` and cached per process.
  text_get           conditional GET of a non-JSON body (CSV, XML, text): the
                     payload is {"url", "text"}, so a parser can read the time
                     from a templated URL
  multi_get          one feed per target (`{target}` in the endpoint): a list of
                     {"target", "cc", "status", "text"}
  linked_get         a listing (RSS/Atom) whose items link to the documents that
                     matter (CAP alerts, warning texts): GET the listing, then
                     each item's links matching `link_pattern` that this process
                     has not fetched for that item version; payload: a list of
                     {"url", "text"}
  paged_get          JSON pages following `next` (RIPE Atlas): the pages'
                     `results` joined, up to `max_pages`
  gdelt_lastupdate   GDELT 2.0: GET the endpoint (lastupdate.txt, "size md5
                     url" lines refreshed every 15 min) → the newest batch file
                     URL matching `file_marker`; skip if it matches the last
                     fetched one (ETag validator slot); else download the
                     zipped batch. No auth.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from worldwatch.config.loader import SourceConfig
from worldwatch.poll.http import USER_AGENT, CacheValidators, FetchResult, conditional_get
from worldwatch.poll.url import build_url

Fetcher = Callable[
    [httpx.AsyncClient, SourceConfig, CacheValidators, int], Awaitable[FetchResult]
]

FETCHERS: dict[str, Fetcher] = {}


def register(kind: str) -> Callable[[Fetcher], Fetcher]:
    def deco(fn: Fetcher) -> Fetcher:
        FETCHERS[kind] = fn
        return fn

    return deco


def get_fetcher(cfg: SourceConfig) -> Fetcher:
    kind = str(cfg.fetch.get("kind", "json_get"))
    if kind not in FETCHERS:
        raise ValueError(f"No fetcher registered for kind {kind!r} (source {cfg.stream_id})")
    return FETCHERS[kind]


@register("json_get")
async def fetch_json_get(
    client: httpx.AsyncClient,
    cfg: SourceConfig,
    validators: CacheValidators,
    now: int,
) -> FetchResult:
    headers: dict[str, str] | None = None
    token = cfg.auth_token()
    scheme = str(cfg.fetch.get("auth_scheme", "Bearer"))
    if token is not None and scheme != "url":
        headers = {"Authorization": f"{scheme} {token}"}
    return await conditional_get(client, build_url(cfg, now), validators, headers=headers)


@register("text_get")
async def fetch_text_get(
    client: httpx.AsyncClient,
    cfg: SourceConfig,
    validators: CacheValidators,
    now: int,
) -> FetchResult:
    url = build_url(cfg, now)
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    if validators.etag:
        headers["If-None-Match"] = validators.etag
    if validators.last_modified:
        headers["If-Modified-Since"] = validators.last_modified
    resp = await client.get(url, headers=headers, timeout=float(cfg.fetch.get("timeout", 60.0)),
                            follow_redirects=True)
    new = CacheValidators(etag=resp.headers.get("ETag", validators.etag),
                          last_modified=resp.headers.get("Last-Modified", validators.last_modified))
    if resp.status_code == 304:
        return FetchResult(304, None, new, not_modified=True)
    resp.raise_for_status()
    return FetchResult(resp.status_code, {"url": url, "text": resp.text}, new)


# per process: (stream, item key) already fetched, so a listing's unchanged items
# are not fetched again every poll (bounded: oldest forgotten first)
_linked_seen: dict[str, dict[str, None]] = {}
_LINKED_MEMORY = 5000


@register("linked_get")
async def fetch_linked_get(
    client: httpx.AsyncClient,
    cfg: SourceConfig,
    validators: CacheValidators,
    now: int,
) -> FetchResult:
    """A listing whose items link to the documents: fetch the new ones.

    An item's version is its link plus its guid/id and date, so a document
    re-issued at the same URL (Kuwait's cap.xml, JTWC's warning texts) is
    fetched again when the listing says it changed."""
    import re

    listing = await conditional_get_text(client, build_url(cfg, now), validators)
    if listing is None:
        return FetchResult(304, None, validators, not_modified=True)
    text, new_validators = listing
    pattern = re.compile(str(cfg.fetch.get("link_pattern", r"https?://[^\s<>\"']+\.xml[^\s<>\"']*")))
    limit = int(cfg.fetch.get("max_items", 40))
    seen = _linked_seen.setdefault(cfg.stream_id, {})
    items = re.findall(r"<(?:item|entry)\b.*?</(?:item|entry)>", text, re.S) or [text]
    todo: list[tuple[str, str]] = []
    for it in items:
        version = " ".join(re.findall(r"<(?:guid|id|pubDate|updated)[^>]*>(.*?)</", it, re.S))
        for url in dict.fromkeys(pattern.findall(it.replace("&amp;", "&"))):
            key = f"{url}|{version}"
            if key not in seen:
                todo.append((key, url))
    out: list[dict[str, Any]] = []
    for key, url in todo[:limit]:
        try:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=30.0,
                                    follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError:
            continue  # fetched again next poll
        out.append({"url": url, "text": resp.text})
        seen[key] = None
        if len(seen) > _LINKED_MEMORY:
            del seen[next(iter(seen))]
    return FetchResult(200, out, new_validators)


async def conditional_get_text(
    client: httpx.AsyncClient, url: str, validators: CacheValidators
) -> tuple[str, CacheValidators] | None:
    """The body of url as text, or None when not modified."""
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    if validators.etag:
        headers["If-None-Match"] = validators.etag
    if validators.last_modified:
        headers["If-Modified-Since"] = validators.last_modified
    resp = await client.get(url, headers=headers, timeout=30.0, follow_redirects=True)
    if resp.status_code == 304:
        return None
    resp.raise_for_status()
    return resp.text, CacheValidators(etag=resp.headers.get("ETag", validators.etag),
                                      last_modified=resp.headers.get("Last-Modified", validators.last_modified))


@register("paged_get")
async def fetch_paged_get(
    client: httpx.AsyncClient,
    cfg: SourceConfig,
    validators: CacheValidators,
    now: int,
) -> FetchResult:
    """JSON pages linked by `next`; their `results` joined."""
    url: str | None = build_url(cfg, now)
    pause = float(cfg.fetch.get("pause_seconds", 0.2))
    results: list[Any] = []
    for i in range(int(cfg.fetch.get("max_pages", 50))):
        if url is None:
            break
        if i:
            await asyncio.sleep(pause)
        resp = await client.get(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
                                timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        page = resp.json()
        results += page.get("results") or []
        url = page.get("next")
    return FetchResult(200, {"results": results}, validators)


# --- several feeds of one source (one stanza, many URLs) ---------------------


@register("multi_get")
async def fetch_multi_get(
    client: httpx.AsyncClient,
    cfg: SourceConfig,
    validators: CacheValidators,
    now: int,
) -> FetchResult:
    """One source published as a feed per region (MeteoAlarm: one per country).

    `[fetch] targets = { germany = "DE", … }` fills `{target}` in the endpoint;
    feeds are fetched one after another with `pause_seconds` between them, and
    the payload is a list of {"target", "cc", "status", "text"}. A failing feed
    is recorded in its entry and never fails the others.
    """
    targets = dict(cfg.fetch.get("targets") or {})
    pause = float(cfg.fetch.get("pause_seconds", 0.3))
    out: list[dict[str, Any]] = []
    for i, (target, cc) in enumerate(sorted(targets.items())):
        if i:
            await asyncio.sleep(pause)
        url = cfg.endpoint.replace("{target}", target)
        try:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=30.0)
            out.append({"target": target, "cc": cc, "status": resp.status_code,
                        "text": resp.text if resp.status_code == 200 else ""})
        except httpx.HTTPError as e:
            out.append({"target": target, "cc": cc, "status": 0, "text": "", "error": str(e)})
    if targets and not any(o["status"] == 200 for o in out):
        raise httpx.HTTPError(f"all {len(out)} feeds failed")
    return FetchResult(200, out, validators)


# --- GDELT 2.0 batch-file fetch ---------------------------------------------


_GDELT_HOST = "data.gdeltproject.org"


def _gdelt_bucket_url(url: str) -> str | None:
    """The same GDELT file straight from its GCS bucket, bypassing the CDN."""
    for scheme in ("http://", "https://"):
        if url.startswith(f"{scheme}{_GDELT_HOST}/"):
            return f"https://storage.googleapis.com/{_GDELT_HOST}/{url[len(scheme) + len(_GDELT_HOST) + 1:]}"
    return None


@register("gdelt_lastupdate")
async def fetch_gdelt_lastupdate(
    client: httpx.AsyncClient,
    cfg: SourceConfig,
    validators: CacheValidators,
    now: int,
) -> FetchResult:
    # follow_redirects: GDELT now 301s http:// → https://, and the listing still
    # names http:// file URLs.
    listing = await client.get(
        cfg.endpoint, headers={"User-Agent": USER_AGENT}, timeout=30.0, follow_redirects=True
    )
    listing.raise_for_status()

    marker = str(cfg.fetch.get("file_marker", ".export.CSV.zip"))
    url = next(
        (
            parts[2]
            for line in listing.text.splitlines()
            if len(parts := line.split()) == 3 and parts[2].endswith(marker)
        ),
        None,
    )
    if url is None:
        raise ValueError(f"lastupdate listing has no {marker!r} entry")

    if validators.etag == url:
        return FetchResult(304, None, validators, not_modified=True)

    resp = await client.get(
        url, headers={"User-Agent": USER_AGENT}, timeout=120.0, follow_redirects=True
    )
    if resp.status_code == 404 and (direct := _gdelt_bucket_url(url)):
        # Some Google CDN edges serve a cached empty 404 for fresh files; the
        # bucket behind data.gdeltproject.org answers directly.
        resp = await client.get(
            direct, headers={"User-Agent": USER_AGENT}, timeout=120.0, follow_redirects=True
        )
    resp.raise_for_status()

    # Batch stamp from the filename: .../YYYYMMDDHHMMSS.export.CSV.zip — the
    # END of the 15-min window this file's events were added in.
    stamp = url.rsplit("/", 1)[-1].split(".", 1)[0]
    payload: dict[str, Any] = {
        "batch_url": url,
        "batch_epoch": _stamp_to_epoch(stamp),
        "content": resp.content,
    }
    return FetchResult(
        resp.status_code,
        payload,
        CacheValidators(etag=url, last_modified=validators.last_modified),
    )


def _stamp_to_epoch(stamp: str) -> int:
    """YYYYMMDDHHMMSS (UTC) → epoch seconds."""
    import calendar

    if len(stamp) != 14 or not stamp.isdigit():
        raise ValueError(f"Bad batch stamp {stamp!r} in file URL")
    return calendar.timegm(
        (
            int(stamp[0:4]),
            int(stamp[4:6]),
            int(stamp[6:8]),
            int(stamp[8:10]),
            int(stamp[10:12]),
            int(stamp[12:14]),
            0,
            0,
            0,
        )
    )


# --- NASA Earthdata granule fetch ------------------------------------------

_URS_BASE = "https://urs.earthdata.nasa.gov"
_GRANULE_TIMEOUT = 300.0  # granules are ~10 MB; generous for a small VPS

# EDL bearer tokens live ~90 days and an account may hold at most 2, so reuse
# an existing one before minting. Cached per username; the lock keeps several
# tile pollers from racing to mint at startup.
_edl_tokens: dict[str, str] = {}
_edl_lock = asyncio.Lock()


async def _edl_token(client: httpx.AsyncClient, cfg: SourceConfig) -> str:
    f = cfg.fetch
    direct = os.environ.get(str(f.get("token_env", "WW_EARTHDATA_TOKEN")))
    if direct:
        return direct

    user_env = str(f.get("user_env", "WW_EARTHDATA_USER"))
    pass_env = str(f.get("pass_env", "WW_EARTHDATA_PASS"))
    user, password = os.environ.get(user_env), os.environ.get(pass_env)
    if not user or not password:
        raise RuntimeError(
            f"Source {cfg.stream_id!r} needs env vars {user_env} + {pass_env} "
            f"(or a pre-minted token in {f.get('token_env', 'WW_EARTHDATA_TOKEN')})"
        )

    urs = str(f.get("urs_base", _URS_BASE))
    async with _edl_lock:
        if user in _edl_tokens:
            return _edl_tokens[user]
        auth = (user, password)
        resp = await client.get(f"{urs}/api/users/tokens", auth=auth, timeout=30.0)
        resp.raise_for_status()
        existing = resp.json()
        if existing:
            token = str(existing[0]["access_token"])
        else:
            resp = await client.post(f"{urs}/api/users/token", auth=auth, timeout=30.0)
            resp.raise_for_status()
            token = str(resp.json()["access_token"])
        _edl_tokens[user] = token
        return token


@register("earthdata_granule")
async def fetch_earthdata_granule(
    client: httpx.AsyncClient,
    cfg: SourceConfig,
    validators: CacheValidators,
    now: int,
) -> FetchResult:
    disc = await client.get(
        cfg.endpoint, headers={"User-Agent": USER_AGENT}, timeout=30.0
    )
    disc.raise_for_status()
    entries = disc.json()["feed"]["entry"]
    if not entries:
        raise ValueError("CMR search returned no granules")
    entry = entries[0]
    granule_id = str(entry["title"])

    if validators.etag == granule_id:
        return FetchResult(304, None, validators, not_modified=True)

    host_mark = str(cfg.fetch.get("data_host_contains", "earthdatacloud.nasa.gov"))
    url = next(
        (
            link["href"]
            for link in entry.get("links", [])
            if str(link.get("href", "")).endswith(".h5") and host_mark in link["href"]
        ),
        None,
    )
    if url is None:
        raise ValueError(f"Granule {granule_id} has no download link on {host_mark!r}")

    token = await _edl_token(client, cfg)
    # follow_redirects: the data host 303s to a presigned S3 URL; httpx drops
    # the Authorization header on the cross-origin hop, which S3 requires.
    resp = await client.get(
        url,
        headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
        timeout=_GRANULE_TIMEOUT,
        follow_redirects=True,
    )
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError:
        if resp.status_code in (401, 403):  # token expired/revoked: re-mint next poll
            _edl_tokens.clear()
        raise

    payload: dict[str, Any] = {
        "granule_id": granule_id,
        "time_start": entry.get("time_start"),
        "content": resp.content,
    }
    return FetchResult(
        resp.status_code,
        payload,
        CacheValidators(etag=granule_id, last_modified=validators.last_modified),
    )
