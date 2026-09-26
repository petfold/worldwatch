"""Normalized observation record — the output of every parser.

Parsers keep the minimal record for the detection path (stream, cell, ts,
value) plus, when the stanza has a [context] table, a slim human-readable
`context` dict (place, headline, link…) for the evidence store. Everything
else is dropped at the door (guardrail 8).
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
    # evidence store only (what happened, for people); detection never reads it
    context: dict[str, object] | None = None
