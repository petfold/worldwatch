"""Layer-0 model family: flavor dispatch, construction from config, (de)serialization.

ONE model family, a few observation flavors (P7). continuous → ContinuousSSM,
count → NegBinomCount. rate/categorical are deferred (P1+); the runner skips
sources with those flavors rather than guessing.

Per-source model hyperparameters live in an optional [<source>.model] TOML
subtable (carried in SourceConfig.extra["model"]); everything has a default so a
plain source stanza still onboards.
"""

from __future__ import annotations

from typing import Protocol

from worldwatch.config.loader import SourceConfig
from worldwatch.layer0 import continuous, count
from worldwatch.layer0.continuous import ContinuousSSM, make_harmonics
from worldwatch.layer0.count import NegBinomCount

SUPPORTED_FLAVORS = ("continuous", "count")

MODEL_VERSION = {
    "continuous": continuous.MODEL_VERSION,
    "count": count.MODEL_VERSION,
}


class Layer0Model(Protocol):
    def update(self, ts: int, value: float) -> float: ...
    def to_bytes(self) -> bytes: ...


def make_model(cfg: SourceConfig) -> Layer0Model:
    """Cold-start a model for a source from its config."""
    mp = dict(cfg.extra.get("model", {}))
    if cfg.flavor == "continuous":
        harmonics = make_harmonics(
            [float(p) for p in mp.get("periods_seconds", [])],
            int(mp.get("n_harmonics", 1)),
        )
        return ContinuousSSM(
            harmonics=harmonics,
            obs_scale=float(mp.get("obs_scale", 1.0)),
            obs_dof=float(mp.get("obs_dof", 4.0)),
            level_var=float(mp.get("level_var", 1e-2)),
            trend_var=float(mp.get("trend_var", 1e-6)),
            seasonal_var=float(mp.get("seasonal_var", 1e-4)),
            time_scale=float(mp.get("time_scale", 3600.0)),
        )
    if cfg.flavor == "count":
        return NegBinomCount(
            seasonal_hour=bool(mp.get("seasonal_hour", False)),
            seasonal_dow=bool(mp.get("seasonal_dow", False)),
        )
    raise ValueError(f"Unsupported flavor {cfg.flavor!r} for source {cfg.stream_id}")


def load_model(flavor: str, blob: bytes) -> Layer0Model:
    """Warm-start a model from a serialized model_state BLOB."""
    if flavor == "continuous":
        return ContinuousSSM.from_bytes(blob)
    if flavor == "count":
        return NegBinomCount.from_bytes(blob)
    raise ValueError(f"Unsupported flavor {flavor!r}")
