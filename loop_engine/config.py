from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from loop_engine.shadow import DEFAULT_THRESHOLDS, parse_thresholds
from swarm_edge_runtime import RUNTIME_PATHS


DEFAULT_LLM_USAGE_PATH = RUNTIME_PATHS.signals_dir / "llm_usage_daily.json"


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default)).strip()))
    except (AttributeError, TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)).strip())
    except (AttributeError, TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class LoopEngineConfig:
    active_universe_size: int = 75
    evaluations_per_cycle: int = 15
    max_llm_calls_per_cycle: int = 3
    max_skeptic_calls_per_cycle: int = 1
    max_daily_llm_calls: int = 24
    candidate_threshold_for_skeptic: float = 0.04
    min_opportunity_score_for_llm: float = 55.0
    skeptic_near_threshold_ratio: float = 0.75
    skeptic_high_confidence: float = 0.85
    estimated_cost_per_call_usd: float = 0.0
    threshold_buckets: tuple[float, ...] = DEFAULT_THRESHOLDS
    time_to_resolution_weight: float = 1.0
    shadow_ledger_enabled: bool = True

    @classmethod
    def from_env(cls) -> "LoopEngineConfig":
        return cls(
            active_universe_size=_env_int(
                "BGL_ACTIVE_UNIVERSE_SIZE",
                _env_int("BGL_UNIVERSE_TARGET_SIZE", 75),
            ),
            evaluations_per_cycle=max(
                1, _env_int("BGL_EVALUATIONS_PER_CYCLE", 15)
            ),
            max_llm_calls_per_cycle=_env_int("BGL_MAX_LLM_CALLS_PER_CYCLE", 3),
            max_skeptic_calls_per_cycle=_env_int("BGL_MAX_SKEPTIC_CALLS_PER_CYCLE", 1),
            max_daily_llm_calls=_env_int("BGL_MAX_DAILY_LLM_CALLS", 24),
            candidate_threshold_for_skeptic=_env_float(
                "BGL_CANDIDATE_THRESHOLD_FOR_SKEPTIC",
                _env_float("BGL_MIN_EDGE_ABS", 0.04),
            ),
            min_opportunity_score_for_llm=_env_float(
                "BGL_MIN_OPPORTUNITY_SCORE_FOR_LLM", 55.0
            ),
            skeptic_near_threshold_ratio=_env_float(
                "BGL_SKEPTIC_NEAR_THRESHOLD_RATIO", 0.75
            ),
            skeptic_high_confidence=_env_float(
                "BGL_SKEPTIC_HIGH_CONFIDENCE", 0.85
            ),
            estimated_cost_per_call_usd=max(
                0.0, _env_float("BGL_ESTIMATED_COST_PER_CALL_USD", 0.0)
            ),
            threshold_buckets=parse_thresholds(
                os.environ.get("BGL_SHADOW_THRESHOLD_BUCKETS")
            ),
            time_to_resolution_weight=max(
                0.0, _env_float("BGL_TIME_TO_RESOLUTION_WEIGHT", 1.0)
            ),
            shadow_ledger_enabled=_env_bool("BGL_SHADOW_LEDGER_ENABLED", True),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "active_universe_size": self.active_universe_size,
            "evaluations_per_cycle": self.evaluations_per_cycle,
            "max_llm_calls_per_cycle": self.max_llm_calls_per_cycle,
            "max_skeptic_calls_per_cycle": self.max_skeptic_calls_per_cycle,
            "max_daily_llm_calls": self.max_daily_llm_calls,
            "candidate_threshold_for_skeptic": self.candidate_threshold_for_skeptic,
            "min_opportunity_score_for_llm": self.min_opportunity_score_for_llm,
            "skeptic_near_threshold_ratio": self.skeptic_near_threshold_ratio,
            "skeptic_high_confidence": self.skeptic_high_confidence,
            "estimated_cost_per_call_usd": self.estimated_cost_per_call_usd,
            "threshold_buckets": list(self.threshold_buckets),
            "time_to_resolution_weight": self.time_to_resolution_weight,
            "shadow_ledger_enabled": self.shadow_ledger_enabled,
        }


class LLMBudget:
    """Tracks per-cycle and persisted daily LLM usage without touching SQLite."""

    def __init__(
        self,
        config: LoopEngineConfig,
        state_path: Path,
        *,
        now: Optional[datetime] = None,
    ) -> None:
        self.config = config
        self.state_path = state_path
        self.now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self.date_utc = self.now.date().isoformat()
        self.llm_calls_used = 0
        self.skeptic_calls_used = 0
        self.daily_calls_at_start = self._load_daily_calls()
        self.daily_calls_used = self.daily_calls_at_start

    def _load_daily_calls(self) -> int:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return 0
        if payload.get("date_utc") != self.date_utc:
            return 0
        try:
            return max(0, int(payload.get("calls_used", 0)))
        except (TypeError, ValueError):
            return 0

    def _persist(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date_utc": self.date_utc,
            "calls_used": self.daily_calls_used,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        tmp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_path.replace(self.state_path)

    def _daily_available(self) -> bool:
        return self.daily_calls_used < self.config.max_daily_llm_calls

    def primary_status(self) -> str:
        if not self._daily_available():
            return "daily_cap_reached"
        if self.llm_calls_used >= self.config.max_llm_calls_per_cycle:
            return "cycle_cap_reached"
        return "available"

    def skeptic_status(self) -> str:
        if not self._daily_available():
            return "daily_cap_reached"
        if self.skeptic_calls_used >= self.config.max_skeptic_calls_per_cycle:
            return "skeptic_cycle_cap_reached"
        return "available"

    def reserve_primary(self) -> bool:
        if self.primary_status() != "available":
            return False
        self.llm_calls_used += 1
        self.daily_calls_used += 1
        self._persist()
        return True

    def reserve_skeptic(self) -> bool:
        if self.skeptic_status() != "available":
            return False
        self.skeptic_calls_used += 1
        self.daily_calls_used += 1
        self._persist()
        return True

    @property
    def estimated_cost(self) -> Optional[float]:
        if self.config.estimated_cost_per_call_usd <= 0:
            return None
        calls = self.llm_calls_used + self.skeptic_calls_used
        return round(calls * self.config.estimated_cost_per_call_usd, 6)
