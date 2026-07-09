"""Normalized observation record — the output of every parser.

Field-drop happens here (guardrail 8): parsers keep only the minimal record
and discard the rest before it touches disk.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Observation:
    """A single normalized observation, ready for raw_ring.

    value is None for pure-event rows (the event's existence is the datum;
    the count flavor folds these into counts per cell/bin downstream).
    """

    stream_id: str
    cell: str
    ts: int  # UTC epoch seconds
    value: float | None = None
    meta: dict[str, object] | None = None
