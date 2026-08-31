from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

CHECKPOINTS = (1.0, 5.0, 30.0, 60.0)


@dataclass
class OpportunityState:
    key: str
    tracking_key: str
    started_ns: int
    started_at_utc: str
    last_seen_ns: int
    book_valid: bool
    economic_executable: bool | None
    execution_validation_status: str
    durability_basis: str
    checkpoints: dict[float, bool | None] = field(
        default_factory=lambda: {checkpoint: None for checkpoint in CHECKPOINTS}
    )
    checkpoint_reasons: dict[float, str | None] = field(
        default_factory=lambda: {checkpoint: None for checkpoint in CHECKPOINTS}
    )
    end_ns: int = 0
    quote_count: int = 0
    best_bid: float | None = None
    best_ask: float | None = None
    best_spread: float | None = None
    worst_spread: float | None = None
    best_depth_usd: float | None = None
    worst_depth_usd: float | None = None
    gross_edge_usd: float | None = None
    fee_source: str = "UNKNOWN"
    fee_rate: float | None = None
    maker_rebate_rate: float | None = None
    maker_rebate_economics: str = "UNKNOWN"
    net_edge_usd: float | None = None
    slippage_source: str = "UNKNOWN"
    rejection_reason: str | None = None
    quote_movements: list[float] = field(default_factory=list)
    adverse_selection: list[float] = field(default_factory=list)
    unknown_reason: str | None = None

    @property
    def lifetime_seconds(self) -> float:
        """Duration bounded by qualifying observations, never timeout padding."""
        return max(
            0.0,
            ((self.end_ns or self.last_seen_ns) - self.started_ns) / 1_000_000_000,
        )

    @property
    def observed_executable_seconds(self) -> float | None:
        if self.economic_executable is not True:
            return None
        return self.lifetime_seconds

    def observe(
        self,
        now_ns: int,
        *,
        book_valid: bool,
        economic_executable: bool | None,
        execution_validation_status: str,
        durability_basis: str,
        bid: float | None,
        ask: float | None,
        depth_usd: float | None,
        gross_edge_usd: float | None,
        movement: float | None,
        adverse_selection: float | None,
    ) -> None:
        self.last_seen_ns = now_ns
        self.quote_count += 1
        self.book_valid = book_valid
        self.economic_executable = economic_executable
        self.execution_validation_status = execution_validation_status
        self.durability_basis = durability_basis
        if bid is not None:
            self.best_bid = bid if self.best_bid is None else max(self.best_bid, bid)
        if ask is not None:
            self.best_ask = ask if self.best_ask is None else min(self.best_ask, ask)
        spread = ask - bid if ask is not None and bid is not None else None
        if spread is not None:
            self.best_spread = (
                spread if self.best_spread is None else min(self.best_spread, spread)
            )
            self.worst_spread = (
                spread if self.worst_spread is None else max(self.worst_spread, spread)
            )
        if depth_usd is not None:
            self.best_depth_usd = (
                depth_usd
                if self.best_depth_usd is None
                else max(self.best_depth_usd, depth_usd)
            )
            self.worst_depth_usd = (
                depth_usd
                if self.worst_depth_usd is None
                else min(self.worst_depth_usd, depth_usd)
            )
        if gross_edge_usd is not None:
            self.gross_edge_usd = gross_edge_usd
        if movement is not None:
            self.quote_movements.append(movement)
        if adverse_selection is not None:
            self.adverse_selection.append(adverse_selection)
        elapsed = (now_ns - self.started_ns) / 1_000_000_000
        for checkpoint in CHECKPOINTS:
            if elapsed >= checkpoint and self.checkpoints[checkpoint] is None:
                self.checkpoints[checkpoint] = True

    def finish_qualification_lost(self, observed_false_ns: int) -> None:
        """Close at the last qualifying observation, not the later false sample."""
        self.end_ns = self.last_seen_ns
        false_elapsed = (observed_false_ns - self.started_ns) / 1_000_000_000
        observed_elapsed = self.lifetime_seconds
        for checkpoint in CHECKPOINTS:
            if self.checkpoints[checkpoint] is not None:
                continue
            if false_elapsed < checkpoint:
                self.checkpoints[checkpoint] = False
            elif observed_elapsed < checkpoint:
                self.checkpoint_reasons[checkpoint] = (
                    "checkpoint_not_observed_before_qualification_lost"
                )

    def finish_observation_gap(self, *, reason: str) -> None:
        """Censor unresolved checkpoints when observation continuity is lost."""
        self.end_ns = self.last_seen_ns
        self.unknown_reason = reason
        for checkpoint in CHECKPOINTS:
            if self.checkpoints[checkpoint] is None:
                self.checkpoint_reasons[checkpoint] = reason


@dataclass(frozen=True)
class ObservationTransition:
    active: OpportunityState | None
    closed: tuple[OpportunityState, ...] = ()


class DurabilityTracker:
    def __init__(self, *, maximum_observation_gap_seconds: float = 2.0) -> None:
        self.maximum_observation_gap_seconds = maximum_observation_gap_seconds
        self.active: dict[str, OpportunityState] = {}

    @staticmethod
    def _interval_key(tracking_key: str, started_ns: int) -> str:
        return hashlib.sha256(f"{tracking_key}:{started_ns}".encode()).hexdigest()

    def observe(
        self,
        tracking_key: str,
        now_ns: int,
        started_at_utc: str,
        *,
        qualifying: bool,
        **kwargs: Any,
    ) -> ObservationTransition:
        closed: list[OpportunityState] = []
        state = self.active.get(tracking_key)
        if state is not None:
            gap_seconds = (now_ns - state.last_seen_ns) / 1_000_000_000
            if gap_seconds >= self.maximum_observation_gap_seconds:
                self.active.pop(tracking_key)
                state.finish_observation_gap(reason="stream_gap_timeout")
                closed.append(state)
                state = None

        if not qualifying:
            if state is not None:
                self.active.pop(tracking_key)
                state.finish_qualification_lost(now_ns)
                closed.append(state)
            return ObservationTransition(active=None, closed=tuple(closed))

        if state is None:
            state = OpportunityState(
                key=self._interval_key(tracking_key, now_ns),
                tracking_key=tracking_key,
                started_ns=now_ns,
                started_at_utc=started_at_utc,
                last_seen_ns=now_ns,
                book_valid=bool(kwargs["book_valid"]),
                economic_executable=kwargs["economic_executable"],
                execution_validation_status=str(
                    kwargs["execution_validation_status"]
                ),
                durability_basis=str(kwargs["durability_basis"]),
            )
            self.active[tracking_key] = state
        state.observe(now_ns, **kwargs)
        return ObservationTransition(active=state, closed=tuple(closed))

    def disappear(
        self,
        tracking_key: str,
        now_ns: int,
        *,
        reason: str | None = None,
        continuous: bool = True,
    ) -> OpportunityState | None:
        del now_ns, continuous
        state = self.active.pop(tracking_key, None)
        if state is not None:
            state.finish_observation_gap(reason=reason or "observation_ended")
        return state

    def expire(
        self, now_ns: int, *, timeout_seconds: float | None = None
    ) -> list[OpportunityState]:
        timeout = (
            self.maximum_observation_gap_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        result = []
        for tracking_key, state in list(self.active.items()):
            if (now_ns - state.last_seen_ns) / 1_000_000_000 >= timeout:
                self.active.pop(tracking_key)
                state.finish_observation_gap(reason="stream_gap_timeout")
                result.append(state)
        return result

    def close_all(self, *, reason: str) -> list[OpportunityState]:
        result = []
        for tracking_key in list(self.active):
            state = self.active.pop(tracking_key)
            state.finish_observation_gap(reason=reason)
            result.append(state)
        return result
