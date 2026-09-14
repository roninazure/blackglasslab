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
    max_daily_llm_calls: int = 1000
    daily_llm_budget_usd: float = 2.0
    emergency_call_ceiling: int = 1000
    budget_warning_50_pct: float = 0.50
    budget_warning_75_pct: float = 0.75
    budget_warning_90_pct: float = 0.90
    budget_reserve_fraction: float = 0.15
    screening_model: str = "claude-haiku-4-5-20251001"
    finalist_model: str = "claude-sonnet-4-6"
    skeptic_model: str = "claude-haiku-4-5-20251001"
    screening_cost_usd: float = 0.0015
    finalist_cost_usd: float = 0.008
    skeptic_cost_usd: float = 0.002
    candidate_threshold_for_skeptic: float = 0.04
    min_opportunity_score_for_llm: float = 55.0
    skeptic_near_threshold_ratio: float = 0.75
    skeptic_high_confidence: float = 0.85
    skeptic_finalist_edge_threshold: float = 0.08
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
            max_daily_llm_calls=_env_int("BGL_MAX_DAILY_LLM_CALLS", 1000),
            daily_llm_budget_usd=max(
                0.0, _env_float("BGL_LLM_DAILY_BUDGET_USD", 2.0)
            ),
            emergency_call_ceiling=_env_int(
                "BGL_LLM_EMERGENCY_CALL_CEILING",
                _env_int("BGL_MAX_DAILY_LLM_CALLS", 1000),
            ),
            budget_warning_50_pct=_env_float("BGL_LLM_BUDGET_WARNING_50_PCT", 0.50),
            budget_warning_75_pct=_env_float("BGL_LLM_BUDGET_WARNING_75_PCT", 0.75),
            budget_warning_90_pct=_env_float("BGL_LLM_BUDGET_WARNING_90_PCT", 0.90),
            budget_reserve_fraction=_env_float("BGL_LLM_BUDGET_RESERVE_FRACTION", 0.15),
            screening_model=os.environ.get(
                "BGL_LLM_SCREENING_MODEL", "claude-haiku-4-5-20251001"
            ).strip(),
            finalist_model=os.environ.get(
                "BGL_LLM_FINALIST_MODEL", "claude-sonnet-4-6"
            ).strip(),
            skeptic_model=os.environ.get(
                "BGL_LLM_SKEPTIC_MODEL", "claude-haiku-4-5-20251001"
            ).strip(),
            screening_cost_usd=max(0.0, _env_float("BGL_LLM_SCREENING_COST_USD", 0.0015)),
            finalist_cost_usd=max(0.0, _env_float("BGL_LLM_FINALIST_COST_USD", 0.008)),
            skeptic_cost_usd=max(0.0, _env_float("BGL_LLM_SKEPTIC_COST_USD", 0.002)),
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
            skeptic_finalist_edge_threshold=max(
                0.0,
                _env_float("BGL_SKEPTIC_FINALIST_EDGE_THRESHOLD", 0.08),
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
            "daily_llm_budget_usd": self.daily_llm_budget_usd,
            "emergency_call_ceiling": self.emergency_call_ceiling,
            "budget_warning_50_pct": self.budget_warning_50_pct,
            "budget_warning_75_pct": self.budget_warning_75_pct,
            "budget_warning_90_pct": self.budget_warning_90_pct,
            "budget_reserve_fraction": self.budget_reserve_fraction,
            "screening_model": self.screening_model,
            "finalist_model": self.finalist_model,
            "skeptic_model": self.skeptic_model,
            "screening_cost_usd": self.screening_cost_usd,
            "finalist_cost_usd": self.finalist_cost_usd,
            "skeptic_cost_usd": self.skeptic_cost_usd,
            "candidate_threshold_for_skeptic": self.candidate_threshold_for_skeptic,
            "min_opportunity_score_for_llm": self.min_opportunity_score_for_llm,
            "skeptic_near_threshold_ratio": self.skeptic_near_threshold_ratio,
            "skeptic_high_confidence": self.skeptic_high_confidence,
            "skeptic_finalist_edge_threshold": self.skeptic_finalist_edge_threshold,
            "estimated_cost_per_call_usd": self.estimated_cost_per_call_usd,
            "threshold_buckets": list(self.threshold_buckets),
            "time_to_resolution_weight": self.time_to_resolution_weight,
            "shadow_ledger_enabled": self.shadow_ledger_enabled,
        }


class LLMBudget:
    """Spend-aware LLM budget with a high emergency call ceiling.

    Reservations are persisted before a provider call.  This makes the dollar
    budget a hard boundary even when provider telemetry arrives after the call.
    The state file is operational telemetry, not trading state.
    """

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
        state = self._load_state()
        self.daily_calls_at_start = int(state.get("calls_used", 0))
        self.daily_calls_used = self.daily_calls_at_start
        self.spent_usd = float(state.get("spent_usd", 0.0) or 0.0)
        self.reserved_usd = float(state.get("reserved_usd", 0.0) or 0.0)
        self.skipped_by_cost = int(state.get("skipped_by_cost", 0) or 0)
        self.skipped_by_emergency = int(state.get("skipped_by_emergency", 0) or 0)
        self.modeled_ev_skipped = float(state.get("modeled_ev_skipped", 0.0) or 0.0)
        self.calls_by_model: dict[str, int] = dict(state.get("calls_by_model", {}) or {})
        self.reservations: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

    def _load_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if payload.get("date_utc") != self.date_utc:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _persist(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date_utc": self.date_utc,
            "calls_used": self.daily_calls_used,
            "spent_usd": round(self.spent_usd, 8),
            "reserved_usd": round(self.reserved_usd, 8),
            "skipped_by_cost": self.skipped_by_cost,
            "skipped_by_emergency": self.skipped_by_emergency,
            "modeled_ev_skipped": round(self.modeled_ev_skipped, 8),
            "calls_by_model": self.calls_by_model,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        tmp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_path.replace(self.state_path)

    def _daily_available(self) -> bool:
        ceiling = min(self.config.max_daily_llm_calls, self.config.emergency_call_ceiling)
        return self.daily_calls_used < ceiling

    @property
    def emergency_ceiling_reached(self) -> bool:
        ceiling = min(self.config.max_daily_llm_calls, self.config.emergency_call_ceiling)
        return self.daily_calls_used >= ceiling

    def _paced_allowance(self) -> float:
        """Budget available at this hour while retaining a later-cycle reserve."""
        hour = self.now.hour + self.now.minute / 60.0
        elapsed = min(1.0, max(0.0, hour / 24.0))
        reserve = self.config.daily_llm_budget_usd * max(
            0.0, min(0.9, self.config.budget_reserve_fraction)
        )
        released = self.config.daily_llm_budget_usd * elapsed
        return min(
            self.config.daily_llm_budget_usd,
            released + reserve,
        )

    def warning_level(self) -> str:
        if self.config.daily_llm_budget_usd <= 0:
            return "DISABLED"
        ratio = self.spent_usd / self.config.daily_llm_budget_usd
        if ratio >= self.config.budget_warning_90_pct:
            return "90%"
        if ratio >= self.config.budget_warning_75_pct:
            return "75%"
        if ratio >= self.config.budget_warning_50_pct:
            return "50%"
        return "NORMAL"

    def remaining_budget(self) -> float:
        return max(0.0, self.config.daily_llm_budget_usd - self.spent_usd - self.reserved_usd)

    def reserve(
        self,
        *,
        model: str,
        estimated_cost_usd: float,
        priority: float = 0.0,
        modeled_ev_usd: float = 0.0,
        operation: str = "forecast",
    ) -> bool:
        cost = max(0.0, float(estimated_cost_usd))
        if operation == "forecast" and self.llm_calls_used >= self.config.max_llm_calls_per_cycle:
            return False
        if operation == "skeptic" and self.skeptic_calls_used >= self.config.max_skeptic_calls_per_cycle:
            return False
        if not self._daily_available():
            self.skipped_by_emergency += 1
            self.modeled_ev_skipped += max(0.0, float(modeled_ev_usd))
            self.events.append({
                "operation": operation, "model": model, "status": "SKIPPED_EMERGENCY",
                "reason": "emergency_call_ceiling", "estimated_cost_usd": cost,
                "modeled_ev_skipped_usd": max(0.0, float(modeled_ev_usd)),
            })
            self._persist()
            return False
        # High-ranked opportunities may consume the released budget; weak early
        # opportunities must leave the reserve for later cycles.
        allowance = self._paced_allowance()
        available_now = max(0.0, allowance - self.spent_usd - self.reserved_usd)
        if cost > available_now and priority < 0.85:
            self.skipped_by_cost += 1
            self.modeled_ev_skipped += max(0.0, float(modeled_ev_usd))
            self.events.append({
                "operation": operation, "model": model, "status": "SKIPPED_COST",
                "reason": "cost_pacing", "estimated_cost_usd": cost,
                "modeled_ev_skipped_usd": max(0.0, float(modeled_ev_usd)),
            })
            self._persist()
            return False
        if cost > self.remaining_budget():
            self.skipped_by_cost += 1
            self.modeled_ev_skipped += max(0.0, float(modeled_ev_usd))
            self.events.append({
                "operation": operation, "model": model, "status": "SKIPPED_COST",
                "reason": "dollar_budget", "estimated_cost_usd": cost,
                "modeled_ev_skipped_usd": max(0.0, float(modeled_ev_usd)),
            })
            self._persist()
            return False
        self.daily_calls_used += 1
        self.reserved_usd += cost
        self.llm_calls_used += int(operation == "forecast")
        self.skeptic_calls_used += int(operation == "skeptic")
        self.calls_by_model[model] = int(self.calls_by_model.get(model, 0)) + 1
        self.reservations.append({"model": model, "operation": operation, "reserved_usd": cost})
        self.events.append({
            "operation": operation, "model": model, "status": "RESERVED",
            "reason": None, "estimated_cost_usd": cost,
            "reserved_cost_usd": cost, "priority": float(priority),
        })
        self._persist()
        return True

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

    def reserve_primary(
        self,
        *,
        model: str | None = None,
        estimated_cost_usd: float | None = None,
        priority: float = 0.0,
        modeled_ev_usd: float = 0.0,
    ) -> bool:
        return self.reserve(
            model=model or self.config.screening_model,
            estimated_cost_usd=(
                self.config.screening_cost_usd
                if estimated_cost_usd is None
                else estimated_cost_usd
            ),
            priority=priority,
            modeled_ev_usd=modeled_ev_usd,
            operation="forecast",
        )

    def reserve_skeptic(
        self,
        *,
        model: str | None = None,
        estimated_cost_usd: float | None = None,
        priority: float = 0.0,
        modeled_ev_usd: float = 0.0,
    ) -> bool:
        return self.reserve(
            model=model or self.config.skeptic_model,
            estimated_cost_usd=(
                self.config.skeptic_cost_usd
                if estimated_cost_usd is None
                else estimated_cost_usd
            ),
            priority=priority,
            modeled_ev_usd=modeled_ev_usd,
            operation="skeptic",
        )

    def record_usage(self, usage: dict[str, Any] | None) -> None:
        if not usage:
            return
        actual = usage.get("estimated_cost_usd")
        if actual is None:
            return
        actual_cost = max(0.0, float(actual))
        reservation = self.reservations.pop(0) if self.reservations else None
        reserved = float(reservation.get("reserved_usd", 0.0)) if reservation else 0.0
        self.reserved_usd = max(0.0, self.reserved_usd - reserved)
        self.spent_usd += actual_cost
        self._persist()

    @property
    def estimated_cost(self) -> Optional[float]:
        return round(self.spent_usd + self.reserved_usd, 6)

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls_used": self.daily_calls_used,
            "llm_calls_used": self.llm_calls_used,
            "skeptic_calls_used": self.skeptic_calls_used,
            "spent_usd": round(self.spent_usd, 8),
            "reserved_usd": round(self.reserved_usd, 8),
            "remaining_budget_usd": round(self.remaining_budget(), 8),
            "warning_level": self.warning_level(),
            "skipped_by_cost": self.skipped_by_cost,
            "skipped_by_emergency": self.skipped_by_emergency,
            "modeled_ev_skipped": round(self.modeled_ev_skipped, 8),
            "calls_by_model": dict(self.calls_by_model),
        }
