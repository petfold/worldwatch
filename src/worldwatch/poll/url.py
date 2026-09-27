"""Endpoint URL templating.

Some feeds encode parameters — and a rolling date range — in the path (e.g. the
Wikimedia pageviews API:
  .../aggregate/{project}/{access}/{agent}/{granularity}/{start}/{end}).

`build_url` fills `{placeholders}` from the source's [parse] table plus computed
`start`/`end` covering a rolling look-back window ending at the poll time (also
as `start_dt`/`end_dt` datetimes, for format specs like `{start_dt:%Y%m%d%H}`,
and `start_iso`/`end_iso`). Feeds
with a plain endpoint (no braces) are returned unchanged. Overlapping windows
are harmless — ingestion dedups on (stream, cell, ts) via the `seen` table.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from worldwatch.config.loader import SourceConfig

# strftime format for start/end by granularity.
_GRANULARITY_FMT = {
    "hourly": "%Y%m%d%H",
    "daily": "%Y%m%d",
    "monthly": "%Y%m01",
}
_DEFAULT_LOOKBACK_SECONDS = 3 * 86400  # 3 days


def build_url(cfg: SourceConfig, now: int) -> str:
    endpoint = cfg.endpoint
    if "{" not in endpoint:
        return endpoint

    fields: dict[str, Any] = dict(cfg.parse)
    granularity = str(fields.get("granularity", "daily"))
    fmt = _GRANULARITY_FMT.get(granularity, "%Y%m%d%H")
    lookback = int(fields.get("lookback_seconds", _DEFAULT_LOOKBACK_SECONDS))

    end_dt = datetime.fromtimestamp(now, tz=UTC)
    start_dt = end_dt - timedelta(seconds=lookback)
    fields.setdefault("start", start_dt.strftime(fmt))
    fields.setdefault("end", end_dt.strftime(fmt))
    fields.setdefault("start_epoch", now - lookback)  # APIs taking epoch seconds (IODA)
    fields.setdefault("end_epoch", now)
    # APIs taking calendar dates (GDACS); end_date is tomorrow, so an API that
    # treats it as exclusive still includes today
    fields.setdefault("start_date", start_dt.strftime("%Y-%m-%d"))
    fields.setdefault("end_date", (end_dt + timedelta(days=1)).strftime("%Y-%m-%d"))
    # datetimes for format specs: "{start_dt:%Y/%Y%m%d}/file_{start_dt:%Y%m%d%H}.dat"
    fields.setdefault("start_dt", start_dt)
    fields.setdefault("end_dt", end_dt)
    fields.setdefault("start_iso", start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
    fields.setdefault("end_iso", end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"))

    try:
        return endpoint.format(**fields)
    except KeyError as e:
        raise ValueError(
            f"Source {cfg.stream_id!r} endpoint needs placeholder {e} "
            f"not present in its [parse] table"
        ) from e
