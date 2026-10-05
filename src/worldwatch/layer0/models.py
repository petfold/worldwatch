"""Layer-0 model family: flavor dispatch, construction from config, (de)serialization.

ONE model family, a few observation flavors (P7). continuous → ContinuousSSM,
count → BayesianCount. rate/categorical are deferred (P1+); the runner skips
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
from worldwatch.layer0.count import LEGACY_SEED, BayesianCount, cell_seed

SUPPORTED_FLAVORS = ("continuous", "count")

MODEL_VERSION = {
    "continuous": continuous.MODEL_VERSION,
    "count": count.MODEL_VERSION,
}


class Layer0Model(Protocol):
    def update(self, ts: int, value: float) -> float: ...
    def to_bytes(self) -> bytes: ...


def make_model(cfg: SourceConfig, cell: str | None = None) -> Layer0Model:
    """Cold-start a model for a source from its config (and, for counts, a
    PIT randomization seed of its own per cell)."""
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
            scale_prior_dof=float(mp.get("scale_prior_dof", 1.0)),
            scale_memory_seconds=float(mp.get("scale_memory_seconds", 7 * 86400.0)),
            quantum=float(mp.get("quantum", 0.0)),
            transform=str(cfg.parse.get("transform", "")) if mp.get("quantum") else "",
            **({"seed": cell_seed(cfg.stream_id, cell)} if cell is not None else {}),
        )
    if cfg.flavor == "count":
        return BayesianCount(
            seasonal_hour=bool(mp.get("seasonal_hour", False)),
            seasonal_dow=bool(mp.get("seasonal_dow", False)),
            memory_seconds=float(mp.get("memory_seconds", 3 * 86400)),
            prior_shape=float(mp.get("prior_shape", 0.5)),
            prior_rate=float(mp.get("prior_rate", 1e-3)),
            **({"seed": cell_seed(cfg.stream_id, cell)} if cell is not None else {}),
        )
    raise ValueError(f"Unsupported flavor {cfg.flavor!r} for source {cfg.stream_id}")


def detection_q(model: Layer0Model, q: float) -> float | None:
    """The q that tail decisions use, stored as surprise.q_detect: for a
    discrete (count) observation the conservative value, else None (= q)."""
    d = getattr(model, "last_detect_q", None)
    return None if d is None or d == q else d


def load_model(
    flavor: str, blob: bytes, key: tuple[str, str] | None = None
) -> Layer0Model:
    """Warm-start a model from a serialized model_state BLOB. `key` is its
    (stream, cell): a count state still on the shared legacy seed is given
    its own."""
    if flavor == "continuous":
        return ContinuousSSM.from_bytes(blob)
    if flavor == "count":
        m = BayesianCount.from_bytes(blob)
        if key is not None and m.seed == LEGACY_SEED:
            m.reseed(cell_seed(*key))
        return m
    raise ValueError(f"Unsupported flavor {flavor!r}")
