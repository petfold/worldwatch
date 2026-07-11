"""Load and validate per-source TOML config stanzas."""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Flavor = Literal["continuous", "count", "rate", "categorical"]
Modality = Literal["physical", "economic", "infrastructural", "informational"]
Status = Literal["nursery", "active", "quarantined", "retired"]


@dataclass(frozen=True)
class SourceConfig:
    stream_id: str
    class_: str
    modality: Modality
    topic_tags: list[str]
    flavor: Flavor
    endpoint: str
    cadence_seconds: int
    parse: dict[str, Any]
    geocode: dict[str, Any]
    fetch: dict[str, Any] = field(default_factory=dict)
    auth_env_var: str | None = None
    status: Status = "nursery"
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def auth_token(self) -> str | None:
        if self.auth_env_var is None:
            return None
        val = os.environ.get(self.auth_env_var)
        if val is None:
            raise RuntimeError(
                f"Source {self.stream_id!r} requires env var {self.auth_env_var!r} (not set)"
            )
        return val


def load_sources(config_dir: Path) -> dict[str, SourceConfig]:
    sources: dict[str, SourceConfig] = {}
    for path in sorted(config_dir.glob("*.toml")):
        raw = tomllib.loads(path.read_text())
        for sid, stanza in raw.items():
            cfg = _parse_stanza(sid, stanza)
            if sid in sources:
                raise ValueError(f"Duplicate stream_id {sid!r} in {path}")
            sources[sid] = cfg
    return sources


def _parse_stanza(stream_id: str, s: dict[str, Any]) -> SourceConfig:
    return SourceConfig(
        stream_id=stream_id,
        class_=str(s["class"]),
        modality=s["modality"],
        topic_tags=list(s.get("topic_tags", [])),
        flavor=s["flavor"],
        endpoint=str(s["endpoint"]),
        cadence_seconds=int(s["cadence_seconds"]),
        parse=dict(s.get("parse", {})),
        geocode=dict(s.get("geocode", {})),
        fetch=dict(s.get("fetch", {})),
        auth_env_var=str(s["auth_env_var"]) if "auth_env_var" in s else None,
        status=s.get("status", "nursery"),
        notes=str(s.get("notes", "")),
        extra={k: v for k, v in s.items() if k not in _KNOWN_KEYS},
    )


_KNOWN_KEYS = {
    "class",
    "modality",
    "topic_tags",
    "flavor",
    "endpoint",
    "cadence_seconds",
    "parse",
    "geocode",
    "fetch",
    "auth_env_var",
    "status",
    "notes",
}
