"""Conditional-GET HTTP fetch with a descriptive User-Agent (guardrail 9)."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

USER_AGENT = (
    "worldwatch/0.1 (global anomaly monitor; "
    "https://github.com/petfold/worldmonitor; contact via repo)"
)


@dataclass(slots=True)
class CacheValidators:
    """Per-source conditional-request state, carried between polls."""

    etag: str | None = None
    last_modified: str | None = None


@dataclass(slots=True)
class FetchResult:
    status: int
    payload: object | None  # parsed JSON, or None on 304/error
    validators: CacheValidators
    not_modified: bool = False


async def conditional_get(
    client: httpx.AsyncClient,
    url: str,
    validators: CacheValidators,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> FetchResult:
    """GET `url`, sending If-None-Match / If-Modified-Since when we have them.

    Returns a FetchResult with parsed JSON on 200, or not_modified=True on 304.
    Raises httpx.HTTPError / httpx.TimeoutException on transport failure so the
    caller can classify and instrument it.
    """
    req_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    if headers:
        req_headers.update(headers)
    if validators.etag:
        req_headers["If-None-Match"] = validators.etag
    if validators.last_modified:
        req_headers["If-Modified-Since"] = validators.last_modified

    resp = await client.get(url, params=params, headers=req_headers, timeout=timeout)

    new_validators = CacheValidators(
        etag=resp.headers.get("ETag", validators.etag),
        last_modified=resp.headers.get("Last-Modified", validators.last_modified),
    )

    if resp.status_code == 304:
        return FetchResult(304, None, new_validators, not_modified=True)

    resp.raise_for_status()
    return FetchResult(resp.status_code, resp.json(), new_validators)
