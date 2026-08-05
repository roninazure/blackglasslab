"""Deterministic model routing for Revenue opportunity evaluation."""
from __future__ import annotations

from dataclasses import dataclass

from loop_engine.config import LoopEngineConfig


@dataclass(frozen=True)
class ModelRoute:
    model: str
    tier: str
    estimated_cost_usd: float


def route_model(
    *,
    opportunity_score: float,
    materially_changed: bool = False,
    skeptic: bool = False,
    config: LoopEngineConfig | None = None,
) -> ModelRoute:
    config = config or LoopEngineConfig.from_env()
    if skeptic:
        return ModelRoute(config.skeptic_model, "skeptic", config.skeptic_cost_usd)
    # Stronger reasoning is reserved for high-quality finalists or markets whose
    # state changed materially since the last evaluation.
    if opportunity_score >= 85.0 or (materially_changed and opportunity_score >= 75.0):
        return ModelRoute(config.finalist_model, "finalist", config.finalist_cost_usd)
    return ModelRoute(config.screening_model, "screening", config.screening_cost_usd)
