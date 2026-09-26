"""Native resolution (ADR 0002 §A): the one fixed bin each stream is modelled at.

Live scoring stores surprise rows at scale NATIVE_SCALE (-1), apart from the
geometric cascade's archive scales (0, 1, 2, …). The width of a native bin
comes from the stanza:

  continuous  0 — each observation is scored on its own, at observation time
  count       [model] native_seconds (default max(300, cadence)) — windows
              bucketed by ARRIVAL time: "reports per window", so a window can
              close right after it ends instead of waiting for stragglers
"""

from __future__ import annotations

from worldwatch.cascade.bins import bin_width
from worldwatch.config.loader import SourceConfig

NATIVE_SCALE = -1
DEFAULT_COUNT_SECONDS = 300


def native_seconds(cfg: SourceConfig) -> int:
    if cfg.flavor != "count":
        return 0
    mp = dict(cfg.extra.get("model", {}))
    return int(mp.get("native_seconds", max(DEFAULT_COUNT_SECONDS, cfg.cadence_seconds)))


def row_seconds(cfg: SourceConfig | None, scale: int) -> int:
    """Width of the bin a surprise row scored (0 = an instantaneous observation)."""
    if scale == NATIVE_SCALE:
        return native_seconds(cfg) if cfg is not None else 0
    return bin_width(scale)
