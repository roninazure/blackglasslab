from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CHECKPOINTS = (1.0, 5.0, 30.0, 60.0)


@dataclass
class OpportunityState:
    key: str
    started_ns: int
    started_at_utc: str
    last_seen_ns: int
    executable: bool
    checkpoints: dict[float, bool | None] = field(default_factory=lambda: {x: None for x in CHECKPOINTS})
    checkpoint_reasons: dict[float, str | None] = field(default_factory=lambda: {x: None for x in CHECKPOINTS})
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
        return max(0.0, ((self.end_ns or self.last_seen_ns) - self.started_ns) / 1_000_000_000)

    def observe(self, now_ns: int, *, executable: bool, bid: float | None, ask: float | None, depth_usd: float | None, movement: float | None, adverse_selection: float | None) -> None:
        self.last_seen_ns = now_ns
        self.quote_count += 1
        self.executable = executable
        if bid is not None: self.best_bid = bid if self.best_bid is None else max(self.best_bid, bid)
        if ask is not None: self.best_ask = ask if self.best_ask is None else min(self.best_ask, ask)
        spread = ask - bid if ask is not None and bid is not None else None
        if spread is not None:
            self.best_spread = spread if self.best_spread is None else min(self.best_spread, spread)
            self.worst_spread = spread if self.worst_spread is None else max(self.worst_spread, spread)
        if depth_usd is not None:
            self.best_depth_usd = depth_usd if self.best_depth_usd is None else max(self.best_depth_usd, depth_usd)
            self.worst_depth_usd = depth_usd if self.worst_depth_usd is None else min(self.worst_depth_usd, depth_usd)
        if movement is not None: self.quote_movements.append(movement)
        if adverse_selection is not None: self.adverse_selection.append(adverse_selection)
        elapsed = (now_ns - self.started_ns) / 1_000_000_000
        for checkpoint in CHECKPOINTS:
            if elapsed >= checkpoint and self.checkpoints[checkpoint] is None:
                self.checkpoints[checkpoint] = bool(executable)

    def finish(self, now_ns: int, *, reason: str | None = None, continuous: bool = True) -> None:
        self.end_ns = now_ns
        self.unknown_reason = reason
        elapsed = (now_ns - self.started_ns) / 1_000_000_000
        for checkpoint in CHECKPOINTS:
            if self.checkpoints[checkpoint] is not None: continue
            if elapsed < checkpoint:
                self.checkpoints[checkpoint] = False
            elif continuous and reason is None:
                self.checkpoints[checkpoint] = bool(self.executable)
            else:
                self.checkpoints[checkpoint] = None
                self.checkpoint_reasons[checkpoint] = reason or "observation_unavailable"


class DurabilityTracker:
    def __init__(self) -> None:
        self.active: dict[str, OpportunityState] = {}

    def observe(self, key: str, now_ns: int, started_at_utc: str, **kwargs: Any) -> OpportunityState:
        state = self.active.get(key)
        if state is None:
            state = self.active[key] = OpportunityState(key, now_ns, started_at_utc, now_ns, kwargs.get("executable", False))
        state.observe(now_ns, **kwargs)
        return state

    def disappear(self, key: str, now_ns: int, *, reason: str | None = None, continuous: bool = True) -> OpportunityState | None:
        state = self.active.pop(key, None)
        if state: state.finish(now_ns, reason=reason, continuous=continuous)
        return state

    def expire(self, now_ns: int, *, timeout_seconds: float = 2.0) -> list[OpportunityState]:
        result = []
        for key, state in list(self.active.items()):
            if (now_ns - state.last_seen_ns) / 1e9 >= timeout_seconds:
                closed = self.disappear(key, now_ns, reason="stream_gap_timeout", continuous=False)
                if closed: result.append(closed)
        return result
