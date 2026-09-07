from __future__ import annotations

import math
import sqlite3
from collections import Counter, deque
from dataclasses import asdict, replace
from datetime import datetime
from hashlib import sha256
from threading import RLock
from typing import Any

from .engine import MAX_SPREAD, qualify
from .entitlements import Feature, Plan, entitlement
from .models import (
    Action,
    Confidence,
    Evidence,
    NormalizedMarket,
    ParallaxSignal,
    PlayType,
    Side,
    SignalSignificance,
    SignalType,
    Venue,
    timestamp,
    utcnow,
)
from .track_record import TrackRecord

PRICE_MOVE_THRESHOLD = 0.05
PRICE_MOVE_HIGH_THRESHOLD = 0.10
SPREAD_MOVE_THRESHOLD = 0.03
SPREAD_MOVE_HIGH_THRESHOLD = 0.08
LIQUIDITY_ABSOLUTE_THRESHOLD = 100.0
LIQUIDITY_PERCENT_THRESHOLD = 50.0
LIQUIDITY_HIGH_ABSOLUTE_THRESHOLD = 500.0
LIQUIDITY_HIGH_PERCENT_THRESHOLD = 100.0
OBSERVATION_HISTORY_LIMIT = 2
SIGNAL_HISTORY_LIMIT = 200
SIGNAL_DEDUPE_COOLDOWN_SECONDS = 15 * 60
SIGNAL_PRIORITY = {
    SignalType.PRICE_MOVE: 0,
    SignalType.SPREAD_MOVE: 1,
    SignalType.LIQUIDITY_MOVE: 2,
}


def _display_price(value: float) -> str:
    cents = value * 100
    return f"{cents:.0f}¢" if cents.is_integer() else f"{cents:.1f}¢"


def _display_quantity(value: float) -> str:
    return f"{value:,.0f}" if value.is_integer() else f"{value:,.2f}"


def _display_signed_price_change(previous_value: float, current_value: float) -> str:
    change = round((current_value - previous_value) * 100, 1)
    prefix = "+" if change > 0 else ""
    return f"{prefix}{change:.0f}¢" if change.is_integer() else f"{prefix}{change:.1f}¢"


def _display_signed_quantity_change(previous_value: float, current_value: float) -> str:
    change = current_value - previous_value
    prefix = "+" if change > 0 else ""
    return f"{prefix}{_display_quantity(change)} contracts"


def _display_window(seconds: float) -> str:
    rounded = round(seconds)
    if rounded < 60:
        return f"{rounded} sec"
    minutes = rounded / 60
    return f"{minutes:.0f} min" if minutes.is_integer() else f"{minutes:.1f} min"


def _display_resolution(value: str | None) -> str | None:
    resolved_at = timestamp(value)
    if resolved_at is None:
        return None
    return f"{resolved_at:%b} {resolved_at.day}, {resolved_at.year}"


def _dedupe_text(value: str) -> str:
    return " ".join(value.casefold().replace("—", " ").replace("-", " ").split())


def _display_title(event_title: str | None, market_title: str) -> str:
    event = (event_title or "").strip()
    market = market_title.strip()
    if not event:
        return market
    event_key = _dedupe_text(event)
    market_key = _dedupe_text(market)
    if event_key and (event_key in market_key or market_key in event_key):
        return market
    return f"{event} — {market}"


def _signal_direction(
    signal_type: SignalType,
    previous_value: float,
    current_value: float,
) -> str:
    if signal_type == SignalType.SPREAD_MOVE:
        return "COMPRESSED" if current_value < previous_value else "WIDENED"
    if signal_type == SignalType.LIQUIDITY_MOVE:
        return "INCREASED" if current_value > previous_value else "DECREASED"
    return "UP" if current_value > previous_value else "DOWN"


def _signal_label(signal_type: SignalType, direction: str) -> str:
    if signal_type == SignalType.SPREAD_MOVE:
        return "Spread Compression" if direction == "COMPRESSED" else "Spread Widening"
    if signal_type == SignalType.LIQUIDITY_MOVE:
        return (
            "Liquidity Increase" if direction == "INCREASED" else "Liquidity Decrease"
        )
    return "Price Move"


def _formatted_previous_current_change(
    signal_type: SignalType,
    previous_value: float,
    current_value: float,
) -> tuple[str, str, str]:
    if signal_type == SignalType.LIQUIDITY_MOVE:
        return (
            f"{_display_quantity(previous_value)} contracts",
            f"{_display_quantity(current_value)} contracts",
            _display_signed_quantity_change(previous_value, current_value),
        )
    return (
        _display_price(previous_value),
        _display_price(current_value),
        _display_signed_price_change(previous_value, current_value),
    )


def _valid_two_sided_book(bid: float | None, ask: float | None) -> bool:
    return (
        bid is not None
        and ask is not None
        and math.isfinite(bid)
        and math.isfinite(ask)
        and 0 < bid < ask < 1
        and ask - bid <= MAX_SPREAD
    )


def _valid_side_books(
    previous: NormalizedMarket,
    current: NormalizedMarket,
    side: Side,
) -> bool:
    name = side.value.lower()
    return _valid_two_sided_book(
        getattr(previous, f"{name}_bid"),
        getattr(previous, f"{name}_ask"),
    ) and _valid_two_sided_book(
        getattr(current, f"{name}_bid"),
        getattr(current, f"{name}_ask"),
    )


class PlayService:
    def __init__(self, store: TrackRecord):
        self.store = store
        self.lock = RLock()
        self.markets: list[NormalizedMarket] = []
        self.evidence: dict[tuple[Venue, str], Evidence] = {}
        self.collection: dict[str, Any] = {}
        self.failures = 0
        self.mode = "live"
        self.last_refresh = None
        self.first_seen: dict[str, str] = {}
        self.observation_history: deque[
            dict[tuple[Venue, str], NormalizedMarket]
        ] = deque(maxlen=OBSERVATION_HISTORY_LIMIT)
        self.observation_times: deque[datetime] = deque(
            maxlen=OBSERVATION_HISTORY_LIMIT
        )
        self.signal_history: deque[ParallaxSignal] = deque(
            maxlen=SIGNAL_HISTORY_LIMIT
        )
        self.last_emitted_signals: dict[
            tuple[Venue, str, SignalType, Side], ParallaxSignal
        ] = {}
        self.pending_liquidity_high: dict[
            tuple[Venue, str, SignalType, Side], tuple[float, float]
        ] = {}

    def replace_inputs(self, markets, evidence=None, collection=None, *, mode="live"):
        if mode not in {"live", "demo"} or any(
            m.demo != (mode == "demo") for m in markets
        ):
            raise ValueError("Live and synthetic inputs must never be mixed")
        with self.lock:
            current = {(m.venue, m.venue_market_id): m for m in markets}
            detected_at = utcnow()
            if self.observation_history:
                previous = self.observation_history[-1]
                for key, market in current.items():
                    if key in previous:
                        self.signal_history.extend(
                            self._detect_signals(
                                previous[key],
                                market,
                                self.observation_times[-1],
                                detected_at,
                            )
                        )
            self.observation_history.append(current)
            self.observation_times.append(detected_at)
            self.markets = list(current.values())
            self.evidence = dict(evidence or {})
            self.collection = collection or {}
            self.mode = mode
            self.last_refresh = detected_at.isoformat()

    @staticmethod
    def _window_seconds(
        previous: NormalizedMarket,
        current: NormalizedMarket,
        previous_detected_at: datetime,
        detected_at: datetime,
    ) -> float:
        previous_at = timestamp(previous.data_timestamp)
        current_at = timestamp(current.data_timestamp)
        if previous_at and current_at and current_at > previous_at:
            return (current_at - previous_at).total_seconds()
        return max(0.0, (detected_at - previous_detected_at).total_seconds())

    @staticmethod
    def _signal_id(
        market: NormalizedMarket,
        signal_type: SignalType,
        side: Side,
        detected_at: datetime,
    ) -> str:
        raw = (
            f"{market.venue}:{market.venue_market_id}:{signal_type}:{side}:"
            f"{detected_at.isoformat()}"
        )
        return f"signal-{sha256(raw.encode()).hexdigest()[:20]}"

    @classmethod
    def _signal(
        cls,
        market: NormalizedMarket,
        signal_type: SignalType,
        side: Side,
        previous_value: float,
        current_value: float,
        window_seconds: float,
        significance: SignalSignificance,
        explanation: str,
        detected_at: datetime,
        *,
        percent_change: float | None = None,
    ) -> ParallaxSignal:
        direction = _signal_direction(signal_type, previous_value, current_value)
        previous_display, current_display, change_display = (
            _formatted_previous_current_change(
                signal_type,
                previous_value,
                current_value,
            )
        )
        return ParallaxSignal(
            id=cls._signal_id(market, signal_type, side, detected_at),
            detected_at=detected_at.isoformat(),
            venue=market.venue,
            market_id=market.venue_market_id,
            market_title=market.title,
            signal_type=signal_type,
            side=side,
            previous_value=previous_value,
            current_value=current_value,
            absolute_change=abs(current_value - previous_value),
            percent_change=percent_change,
            observation_window_seconds=window_seconds,
            significance=significance,
            explanation=explanation,
            market_url=market.source_url,
            market_reference=market.slug or market.venue_market_id,
            resolution_time=market.resolution_time,
            event_title=market.event_title,
            display_title=_display_title(market.event_title, market.title),
            category=market.category,
            direction=direction,
            signal_label=_signal_label(signal_type, direction),
            signal_strength=significance.value,
            formatted_previous_value=previous_display,
            formatted_current_value=current_display,
            formatted_change=change_display,
            formatted_window=_display_window(window_seconds),
            resolution_label=_display_resolution(market.resolution_time),
        )

    @staticmethod
    def _signal_key(
        market: NormalizedMarket,
        signal_type: SignalType,
        side: Side,
    ) -> tuple[Venue, str, SignalType, Side]:
        return (market.venue, market.venue_market_id, signal_type, side)

    @staticmethod
    def _percent_change(previous_value: float, current_value: float) -> float | None:
        if previous_value <= 0:
            return None
        return (current_value - previous_value) / previous_value * 100

    @staticmethod
    def _liquidity_significant(
        previous_value: float,
        current_value: float,
    ) -> tuple[bool, bool, float | None]:
        absolute = abs(current_value - previous_value)
        percent = PlayService._percent_change(previous_value, current_value)
        percent_abs = abs(percent) if percent is not None else None
        material = (
            percent_abs is not None
            and absolute >= LIQUIDITY_ABSOLUTE_THRESHOLD
            and percent_abs >= LIQUIDITY_PERCENT_THRESHOLD
        )
        high = (
            material
            and absolute >= LIQUIDITY_HIGH_ABSOLUTE_THRESHOLD
            and percent_abs is not None
            and percent_abs >= LIQUIDITY_HIGH_PERCENT_THRESHOLD
        )
        return material, high, percent

    @staticmethod
    def _same_direction(
        prior_start: float,
        prior_current: float,
        current_value: float,
    ) -> bool:
        prior_direction = prior_current - prior_start
        current_direction = current_value - prior_start
        return (
            prior_direction != 0
            and current_direction != 0
            and (prior_direction > 0) == (current_direction > 0)
        )

    def _should_emit_signal(
        self,
        signal: ParallaxSignal,
        threshold: float,
        detected_at: datetime,
    ) -> bool:
        key = (signal.venue, signal.market_id, signal.signal_type, signal.side)
        last = self.last_emitted_signals.get(key)
        if last is None:
            self.last_emitted_signals[key] = signal
            return True
        if (
            last.significance == SignalSignificance.MATERIAL
            and signal.significance == SignalSignificance.HIGH
        ):
            self.last_emitted_signals[key] = signal
            return True
        if abs(signal.current_value - last.current_value) >= threshold:
            self.last_emitted_signals[key] = signal
            return True
        last_at = timestamp(last.detected_at)
        if (
            last_at is not None
            and (detected_at - last_at).total_seconds()
            >= SIGNAL_DEDUPE_COOLDOWN_SECONDS
        ):
            self.last_emitted_signals[key] = signal
            return True
        return False

    def _append_signal(
        self,
        signals: list[ParallaxSignal],
        signal: ParallaxSignal,
        threshold: float,
        detected_at: datetime,
    ) -> None:
        if self._should_emit_signal(signal, threshold, detected_at):
            signals.append(signal)

    @staticmethod
    def _dedupe_threshold(signal_type: SignalType) -> float:
        if signal_type == SignalType.PRICE_MOVE:
            return PRICE_MOVE_THRESHOLD
        if signal_type == SignalType.SPREAD_MOVE:
            return SPREAD_MOVE_THRESHOLD
        return LIQUIDITY_ABSOLUTE_THRESHOLD

    @staticmethod
    def _best_signal(signals: list[ParallaxSignal]) -> ParallaxSignal | None:
        if not signals:
            return None
        return min(
            signals,
            key=lambda signal: (
                SIGNAL_PRIORITY[signal.signal_type],
                -signal.absolute_change,
            ),
        )

    def _surface_window_signals(
        self,
        candidates: list[ParallaxSignal],
        detected_at: datetime,
    ) -> list[ParallaxSignal]:
        signal = self._best_signal(candidates)
        if signal is None:
            return []
        surfaced: list[ParallaxSignal] = []
        self._append_signal(
            surfaced,
            signal,
            self._dedupe_threshold(signal.signal_type),
            detected_at,
        )
        return surfaced

    def _detect_signals(
        self,
        previous: NormalizedMarket,
        current: NormalizedMarket,
        previous_detected_at: datetime,
        detected_at: datetime,
    ) -> list[ParallaxSignal]:
        signals: list[ParallaxSignal] = []
        window = self._window_seconds(
            previous, current, previous_detected_at, detected_at
        )
        window_text = f"{window:.0f} seconds"
        for side in Side:
            name = side.value.lower()
            prior_bid = getattr(previous, f"{name}_bid")
            current_bid = getattr(current, f"{name}_bid")
            prior_ask = getattr(previous, f"{name}_ask")
            current_ask = getattr(current, f"{name}_ask")
            valid_books = _valid_side_books(previous, current, side)
            if valid_books and prior_ask is not None and current_ask is not None:
                change = abs(current_ask - prior_ask)
                if change >= PRICE_MOVE_THRESHOLD:
                    significance = (
                        SignalSignificance.HIGH
                        if change >= PRICE_MOVE_HIGH_THRESHOLD
                        else SignalSignificance.MATERIAL
                    )
                    signals.append(
                        self._signal(
                            current,
                            SignalType.PRICE_MOVE,
                            side,
                            prior_ask,
                            current_ask,
                            window,
                            significance,
                            f"{side} moved from {_display_price(prior_ask)} to "
                            f"{_display_price(current_ask)} over the last {window_text}.",
                            detected_at,
                        )
                    )

            if (
                valid_books
                and prior_bid is not None
                and prior_ask is not None
                and current_bid is not None
                and current_ask is not None
            ):
                prior_spread = max(0.0, prior_ask - prior_bid)
                current_spread = max(0.0, current_ask - current_bid)
                change = abs(current_spread - prior_spread)
                if change >= SPREAD_MOVE_THRESHOLD:
                    significance = (
                        SignalSignificance.HIGH
                        if change >= SPREAD_MOVE_HIGH_THRESHOLD
                        else SignalSignificance.MATERIAL
                    )
                    direction = (
                        "compressed" if current_spread < prior_spread else "widened"
                    )
                    signals.append(
                        self._signal(
                            current,
                            SignalType.SPREAD_MOVE,
                            side,
                            prior_spread,
                            current_spread,
                            window,
                            significance,
                            f"The {side} bid/ask spread {direction} from "
                            f"{_display_price(prior_spread)} to "
                            f"{_display_price(current_spread)}.",
                            detected_at,
                        )
                    )

            prior_levels = previous.executable_depth.get(side.value, ())
            current_levels = current.executable_depth.get(side.value, ())
            if valid_books and prior_levels and current_levels:
                prior_depth = float(prior_levels[0][1])
                current_depth = float(current_levels[0][1])
                key = self._signal_key(current, SignalType.LIQUIDITY_MOVE, side)
                pending = self.pending_liquidity_high.get(key)
                if pending is not None:
                    pending_start, pending_current = pending
                    _, persistent_high, persistent_percent = (
                        self._liquidity_significant(pending_start, current_depth)
                    )
                    if persistent_high and self._same_direction(
                        pending_start, pending_current, current_depth
                    ):
                        direction = (
                            "increased"
                            if current_depth > pending_start
                            else "decreased"
                        )
                        signals.append(
                            self._signal(
                                current,
                                SignalType.LIQUIDITY_MOVE,
                                side,
                                pending_start,
                                current_depth,
                                window,
                                SignalSignificance.HIGH,
                                f"Executable {side} depth {direction} from "
                                f"{_display_quantity(pending_start)} to "
                                f"{_display_quantity(current_depth)} contracts.",
                                detected_at,
                                percent_change=persistent_percent,
                            )
                        )
                        self.pending_liquidity_high.pop(key, None)
                        continue
                    if not self._same_direction(
                        pending_start, pending_current, current_depth
                    ):
                        self.pending_liquidity_high.pop(key, None)
                        continue
                    self.pending_liquidity_high.pop(key, None)

                material, high_candidate, percent = self._liquidity_significant(
                    prior_depth, current_depth
                )
                if material:
                    significance = SignalSignificance.MATERIAL
                    if high_candidate:
                        self.pending_liquidity_high[key] = (
                            prior_depth,
                            current_depth,
                        )
                    direction = (
                        "increased" if current_depth > prior_depth else "decreased"
                    )
                    signals.append(
                        self._signal(
                            current,
                            SignalType.LIQUIDITY_MOVE,
                            side,
                            prior_depth,
                            current_depth,
                            window,
                            significance,
                            f"Executable {side} depth {direction} from "
                            f"{_display_quantity(prior_depth)} to "
                            f"{_display_quantity(current_depth)} contracts.",
                            detected_at,
                            percent_change=percent,
                        )
                    )
        return self._surface_window_signals(signals, detected_at)

    def _plays(self):
        now = utcnow()
        result = []
        with self.lock:
            for market in self.markets:
                evidence = self.evidence.get((market.venue, market.venue_market_id))
                for side in Side:
                    try:
                        play = qualify(market, side, evidence, now=now)
                        created = self.first_seen.setdefault(play.id, play.created_at)
                        play = replace(play, created_at=created)
                        if play.suggested_action == Action.BUY and not play.demo:
                            # Persist before returning an actionable play to any consumer.
                            self.store.publish(market, side, evidence, now=now)
                        result.append(play)
                    except (ValueError, TypeError, ArithmeticError, sqlite3.Error):
                        self.failures += 1
                        # Fail closed: never return an unrecorded live BUY.
            return result

    @staticmethod
    def _view(play, plan):
        view = play.as_dict()
        if not entitlement(plan).permits(Feature.DETAILS):
            for key in (
                "evidence",
                "expected_value",
                "expected_return",
                "decision_reasons",
                "reason_factors",
            ):
                view.pop(key, None)
            view["verdict"].pop("supporting_reasons", None)
        return view

    def plays(self, plan: Plan = Plan.EXPLORER, **filters):
        allowed = {
            "venue",
            "confidence",
            "action",
            "play_type",
            "resolution_horizon",
            "minimum_edge",
        }
        if set(filters) - allowed:
            raise ValueError("Unknown filter")
        advanced = {
            k: v for k, v in filters.items() if k != "venue" and v not in (None, "")
        }
        if advanced and not entitlement(plan).permits(Feature.ADVANCED_FILTERS):
            raise PermissionError("Advanced filters require PRO")
        for key, enum in (
            ("venue", Venue),
            ("confidence", Confidence),
            ("action", Action),
            ("play_type", PlayType),
        ):
            if filters.get(key):
                enum(filters[key])
        for key in ("resolution_horizon", "minimum_edge"):
            if filters.get(key) not in (None, ""):
                value = float(filters[key])
                if not math.isfinite(value) or value < 0:
                    raise ValueError(f"{key} must be finite and nonnegative")
                filters[key] = value
        rows = self._plays()
        for key, attr in (
            ("venue", "venue"),
            ("confidence", "confidence_band"),
            ("action", "suggested_action"),
            ("play_type", "play_type"),
        ):
            if filters.get(key):
                rows = [p for p in rows if getattr(p, attr) == filters[key]]
        horizon, edge = filters.get("resolution_horizon"), filters.get("minimum_edge")
        if horizon not in (None, ""):
            rows = [
                p
                for p in rows
                if p.estimated_time_to_resolution is not None
                and 0 < p.estimated_time_to_resolution <= float(horizon) * 3600
            ]
        if edge not in (None, ""):
            rows = [
                p
                for p in rows
                if p.edge_points is not None and p.edge_points >= float(edge)
            ]
        rows.sort(
            key=lambda p: (
                {Action.BUY: 0, Action.WATCH: 1, Action.PASS: 2}[p.suggested_action],
                p.venue,
                p.market_id,
                p.side,
            )
        )
        # Interleave venues so the limited Explorer feed visibly covers both.
        first = [p for p in rows if p.venue == Venue.POLYMARKET]
        second = [p for p in rows if p.venue == Venue.KALSHI]
        interleaved = [
            p
            for i in range(max(len(first), len(second)))
            for group in (first, second)
            if i < len(group)
            for p in [group[i]]
        ]
        limited = interleaved[: entitlement(plan).play_limit]
        return {
            "mode": self.mode,
            "total": len(rows),
            "limit": entitlement(plan).play_limit,
            "items": [self._view(p, plan) for p in limited],
            "as_of": utcnow().isoformat(),
        }

    def play(self, play_id: str, plan: Plan = Plan.EXPLORER):
        if not entitlement(plan).permits(Feature.DETAILS):
            raise PermissionError("Full play details require PRO")
        for item in self._plays():
            if item.id == play_id:
                return self._view(item, plan)
        raise KeyError(play_id)

    def market_views(self, plan: Plan = Plan.EXPLORER):
        with self.lock:
            rows = [asdict(m) for m in self.markets]
        if not entitlement(plan).permits(Feature.DETAILS):
            keys = {
                "venue",
                "venue_market_id",
                "title",
                "outcomes",
                "status",
                "yes_bid",
                "yes_ask",
                "no_bid",
                "no_ask",
                "data_timestamp",
                "book_timestamp",
                "resolution_time",
                "demo",
            }
            rows = [{k: v for k, v in row.items() if k in keys} for row in rows]
        return rows

    def signals(self, plan: Plan = Plan.EXPLORER):
        with self.lock:
            rows = list(self.signal_history)
        rank = {SignalSignificance.HIGH: 1, SignalSignificance.MATERIAL: 0}
        rows.sort(key=lambda row: (rank[row.significance], row.detected_at), reverse=True)
        limit = min(5, entitlement(plan).play_limit)
        if entitlement(plan).permits(Feature.DETAILS):
            limit = entitlement(plan).play_limit
        return {
            "mode": self.mode,
            "total": len(rows),
            "limit": limit,
            "items": [row.as_dict() for row in rows[:limit]],
            "as_of": utcnow().isoformat(),
        }

    def health(self):
        plays = self._plays()
        metrics = Counter(
            {
                "plays_generated": len(plays),
                "play_generation_failures": self.failures,
                "BUY": 0,
                "WATCH": 0,
                "PASS": 0,
                "stale_data_rejections": 0,
                "liquidity_rejections": 0,
                "confidence_rejections": 0,
            }
        )
        for play in plays:
            metrics[play.suggested_action] += 1
            for code in play.verdict.failed_gates:
                if code in {"stale_data", "liquidity", "confidence"}:
                    metrics[f"{code}_rejections"] += 1
        summary = self.store.summary()
        metrics.update(
            {f"published_outcomes_{k}": summary[k] for k in ("wins", "losses", "voids")}
        )
        metrics.update(
            {
                f"{v}.markets_observed": sum(m.venue == v for m in self.markets)
                for v in Venue
            }
        )
        return {
            "status": "ok"
            if self.markets
            and not self.collection.get("errors")
            and not self.failures
            and not metrics["stale_data_rejections"]
            else "degraded",
            "mode": self.mode,
            "last_refresh": self.last_refresh,
            "metrics": dict(metrics),
            "collection": self.collection,
            "live_orders": 0,
            "execution_enabled": False,
        }

    def venues(self):
        return [
            {
                "venue": v,
                "label": "Polymarket US" if v == Venue.POLYMARKET else "Kalshi",
                "markets_observed": sum(m.venue == v for m in self.markets),
                "intelligence": True,
                "execution_enabled": False,
            }
            for v in Venue
        ]

    def alert_candidates(self, plan: Plan):
        if not entitlement(plan).permits(Feature.ALERTS):
            raise PermissionError("Alerts require PRO")
        return self.plays(plan, action="BUY")["items"]
