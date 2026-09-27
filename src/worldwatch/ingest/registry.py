"""The parser registry (its own module, so parser modules can import it in any order)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from worldwatch.config.loader import SourceConfig
from worldwatch.ingest.models import Observation

Parser = Callable[[Any, SourceConfig], list[Observation]]

PARSERS: dict[str, Parser] = {}

# Sentinel: parser could not derive a timestamp; the poller substitutes poll time.
_NOW_SENTINEL = -1


def register(fmt: str) -> Callable[[Parser], Parser]:
    def deco(fn: Parser) -> Parser:
        PARSERS[fmt] = fn
        return fn

    return deco
