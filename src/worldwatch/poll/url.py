"""Endpoint URL templating.

Some feeds encode parameters — and a rolling date range — in the path (e.g. the
Wikimedia pageviews API:
  .../aggregate/{project}/{access}/{agent}/{granularity}/{start}/{end}).

`build_url` fills `{placeholders}` from the source's [parse] table plus computed
`start`/`end` covering a rolling look-back window ending at the poll time. Feeds
with a plain endpoint (no braces) are returned unchanged. Overlapping windows
are harmless — raw_ring dedups on its primary key.
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

    try:
        return endpoint.format(**fields)
    except KeyError as e:
        raise ValueError(
            f"Source {cfg.stream_id!r} endpoint needs placeholder {e} "
            f"not present in its [parse] table"
        ) from e
