"""Phase 3 opportunity loop primitives."""

from .config import LLMBudget, LoopEngineConfig
from .opportunity import OpportunityScore, score_opportunity
from .prompts import build_forecast_prompts, classify_market, prompt_family_for_category
from .skeptic import SkepticReview, should_request_skeptic

__all__ = [
    "LLMBudget",
    "LoopEngineConfig",
    "OpportunityScore",
    "SkepticReview",
    "build_forecast_prompts",
    "classify_market",
    "prompt_family_for_category",
    "score_opportunity",
    "should_request_skeptic",
]
