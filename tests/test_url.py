"""Endpoint URL templating."""

import dataclasses

import pytest

from worldwatch.poll.url import build_url

# 2026-07-10T00:00:00Z
NOW = 1_783_641_600


def test_plain_endpoint_unchanged(sources):
    cfg = sources["usgs_seismic"]
    assert build_url(cfg, NOW) == cfg.endpoint


def test_wikipedia_endpoint_filled(sources):
    cfg = sources["wikipedia_pageviews"]
    url = build_url(cfg, NOW)
    assert "{" not in url  # all placeholders resolved
    assert "en.wikipedia.org/all-access/all-agents/hourly/" in url
    # start/end are 10-digit YYYYMMDDHH, end is the poll hour, start is earlier
    start, end = url.rsplit("/", 2)[-2:]
    assert len(start) == len(end) == 10
    assert end == "2026071000"
    assert start < end  # 3-day look-back


def test_lookback_respected(sources):
    cfg = sources["wikipedia_pageviews"]
    one_day = dataclasses.replace(cfg, parse={**cfg.parse, "lookback_seconds": 86400})
    url = build_url(one_day, NOW)
    start = url.rsplit("/", 2)[-2]
    assert start == "2026070900"  # exactly one day before end


def test_missing_placeholder_raises(sources):
    cfg = sources["wikipedia_pageviews"]
    broken = dataclasses.replace(
        cfg, parse={"format": "wikimedia_pageviews", "granularity": "hourly"}
    )
    with pytest.raises(ValueError, match="placeholder"):
        build_url(broken, NOW)
