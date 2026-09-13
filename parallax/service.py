from __future__ import annotations

import math
import sqlite3
from collections import Counter, deque
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from hashlib import sha256
from threading import RLock
from typing import Any

from .alerts import AlertDeliveryStore, AlertDispatcher
from .engine import MAX_SPREAD, qualify
from .evidence import EvidenceEngine
from .entitlements import Feature, Plan, entitlement
from .inbox import (
    ATTENTION_PRIORITY,
    INBOX_ACTIVE_LIMIT,
    InboxStore,
    InboxUpsert,
    default_inbox_store,
)
from .models import (
    Action,
    AttentionClass,
    Confidence,
    Evidence,
    InboxStatus,
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
from .social import SocialPublisher
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
PUBLISHABLE_SIGNAL_MAX_AGE_SECONDS = 5 * 60
NEW_MARKET_PUBLICATION_BLOCK_SECONDS = 15 * 60
PUBLISHABLE_SIGNAL_LIMIT = 10
EXPLORER_PUBLISHABLE_SIGNAL_LIMIT = 3
INBOX_SIGNAL_MAX_AGE_SECONDS = PUBLISHABLE_SIGNAL_MAX_AGE_SECONDS
SIGNAL_PRIORITY = {
    SignalType.PRICE_MOVE: 0,
    SignalType.SPREAD_MOVE: 1,
    SignalType.LIQUIDITY_MOVE: 2,
}
SPORTS_CATEGORIES = {
    "baseball",
    "basketball",
    "football",
    "hockey",
    "mma",
    "soccer",
    "sports",
    "tennis",
}
RETAIL_STAKES = (25.0, 50.0, 100.0)
TEMPORAL_SIGNAL_TYPES = {
    SignalType.PRICE_MOVE,
    SignalType.SPREAD_MOVE,
    SignalType.LIQUIDITY_MOVE,
}


def _display_price(value: float) -> str:
    cents = round(value * 100, 10)
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


def _display_money(value: float) -> str:
    return f"${value:,.2f}"


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


def _venue_label(venue: Venue) -> str:
    return "Polymarket" if venue == Venue.POLYMARKET else "Kalshi"


def _is_sports_category(category: str | None) -> bool:
    normalized = (category or "").casefold()
    return any(label in normalized for label in SPORTS_CATEGORIES)


def _has_publishable_context(signal: ParallaxSignal) -> bool:
    required = (
        signal.display_title,
        signal.signal_label,
        signal.direction,
        signal.formatted_previous_value,
        signal.formatted_current_value,
        signal.formatted_change,
        signal.formatted_window,
        signal.signal_strength,
    )
    if not all(str(value).strip() for value in required):
        return False
    if _is_sports_category(signal.category):
        return bool(signal.event_title and signal.event_title.strip())
    return True


def _social_preview(signal: ParallaxSignal) -> str:
    return "\n".join(
        (
            "PARALLAX SIGNAL",
            "",
            _venue_label(signal.venue),
            str(signal.display_title),
            "",
            str(signal.signal_label),
            f"{signal.side}: {signal.formatted_previous_value} -> {signal.formatted_current_value}",
            f"{signal.formatted_change} in {signal.formatted_window}",
            f"Market Activity Strength: {signal.signal_strength}",
            "",
            "Observed market movement. Not a BUY recommendation.",
        )
    )


def _market_opened_at(market: NormalizedMarket) -> datetime | None:
    for key in (
        "opened_at",
        "open_time",
        "created_at",
        "listed_at",
        "published_at",
        "first_opened_at",
    ):
        opened_at = timestamp(market.original_metadata.get(key))
        if opened_at is not None:
            return opened_at
    return None


def _market_age_seconds(
    market: NormalizedMarket,
    now: datetime,
) -> float | None:
    opened_at = _market_opened_at(market)
    if opened_at is not None and now >= opened_at:
        return (now - opened_at).total_seconds()
    return None


def _risk_reward_label(price: float | None) -> str:
    if price is None:
        return "UNKNOWN"
    if price >= 0.75:
        return "LOW UPSIDE / HIGH PRICE"
    if price <= 0.35:
        return "HIGH UPSIDE / LOW PRICE"
    return "BALANCED"


def _economics(price: float | None, label: str) -> dict[str, Any]:
    examples = []
    for stake in RETAIL_STAKES:
        if price is None or not math.isfinite(price) or not 0 < price < 1:
            examples.append(
                {
                    "stake": stake,
                    "available": False,
                    "reason": "No valid executable buy price is available.",
                }
            )
            continue
        shares = stake / price
        payout = shares
        examples.append(
            {
                "stake": stake,
                "price_paid": price,
                "contracts_or_shares": shares,
                "payout_if_correct": payout,
                "gross_profit_if_correct": payout - stake,
                "maximum_loss": stake,
                "before_fees_costs": True,
            }
        )
    if price is None:
        explanation = "No valid executable buy price is available."
    else:
        per_contract_gain = max(0.0, 1 - price)
        explanation = (
            f"At {_display_price(price)}, approximately {_display_price(price)} is "
            f"risked to make {_display_price(per_contract_gain)} per contract if correct."
        )
    return {
        "label": label,
        "examples": examples,
        "risk_reward_label": _risk_reward_label(price),
        "risk_reward_explanation": explanation,
        "note": "Before fees/costs.",
    }


class PlayService:
    def __init__(
        self,
        store: TrackRecord,
        inbox_store: InboxStore | None = None,
        alert_dispatcher: AlertDispatcher | None = None,
        social_publisher: SocialPublisher | None = None,
        evidence_engine: EvidenceEngine | None = None,
    ):
        self.store = store
        self.inbox_store = inbox_store or default_inbox_store()
        self.alert_dispatcher = alert_dispatcher or AlertDispatcher(
            AlertDeliveryStore(self.inbox_store.path)
        )
        self.social = social_publisher or SocialPublisher()
        self.evidence_engine = evidence_engine or EvidenceEngine()
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
        self.inbox_suppressed = 0

    def replace_inputs(self, markets, evidence=None, collection=None, *, mode="live"):
        if mode not in {"live", "demo"} or any(
            m.demo != (mode == "demo") for m in markets
        ):
            raise ValueError("Live and synthetic inputs must never be mixed")
        with self.lock:
            collected_evidence = {}
            if evidence is None and isinstance(collection, dict):
                collected_evidence = collection.pop("_evidence", {})
                if not isinstance(collected_evidence, dict):
                    collected_evidence = {}
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
            self.evidence = dict(evidence or collected_evidence)
            if mode == "live" and evidence is None:
                for market in self.markets:
                    if (market.venue, market.venue_market_id) in self.evidence:
                        continue
                    proof = self.evidence_engine.assess(market)
                    if proof is not None:
                        self.evidence[(market.venue, market.venue_market_id)] = proof
            self.collection = collection or {}
            self.mode = mode
            self.last_refresh = detected_at.isoformat()
        self.refresh_inbox()

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

    def _current_market_snapshot(
        self,
        signal: ParallaxSignal,
        market: NormalizedMarket | None,
        now: datetime,
    ) -> dict[str, Any]:
        if market is None:
            return {
                "status": None,
                "resolution_time": signal.resolution_time,
                "market_age_seconds": None,
            }
        yes_spread = (
            market.yes_ask - market.yes_bid
            if market.yes_bid is not None and market.yes_ask is not None
            else None
        )
        no_spread = (
            market.no_ask - market.no_bid
            if market.no_bid is not None and market.no_ask is not None
            else None
        )
        return {
            "yes_bid": market.yes_bid,
            "yes_ask": market.yes_ask,
            "no_bid": market.no_bid,
            "no_ask": market.no_ask,
            "spread": {
                "YES": yes_spread,
                "NO": no_spread,
            },
            "current_executable_buy_prices": {
                "YES": market.yes_ask,
                "NO": market.no_ask,
            },
            "market_age_seconds": _market_age_seconds(market, now),
            "resolution_time": market.resolution_time,
            "status": market.status,
        }

    def _publishability_reasons(
        self,
        signal: ParallaxSignal,
        market: NormalizedMarket | None,
        now: datetime,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if signal.significance != SignalSignificance.HIGH:
            reasons.append("NOT_HIGH_MARKET_ACTIVITY")
        if signal.signal_type == SignalType.LIQUIDITY_MOVE:
            reasons.append("LIQUIDITY_ONLY_NOT_DIRECTIONAL")
        detected_at = timestamp(signal.detected_at)
        cutoff = now - timedelta(seconds=PUBLISHABLE_SIGNAL_MAX_AGE_SECONDS)
        if detected_at is None or detected_at < cutoff:
            reasons.append("STALE_SIGNAL")
        resolved_at = timestamp(signal.resolution_time)
        if resolved_at is not None and resolved_at <= now:
            reasons.append("RESOLVED_OR_EXPIRED_MARKET")
        if market is not None:
            if market.status != "OPEN":
                reasons.append("MARKET_NOT_OPEN")
            age = _market_age_seconds(market, now)
            if (
                age is not None
                and age < NEW_MARKET_PUBLICATION_BLOCK_SECONDS
                and signal.signal_type in TEMPORAL_SIGNAL_TYPES
            ):
                reasons.append("NEW_MARKET_PRICE_DISCOVERY")
        if not _has_publishable_context(signal):
            reasons.append("INSUFFICIENT_PUBLIC_CONTEXT")
        return tuple(dict.fromkeys(reasons))

    def _inbox_expiry(self, signal: ParallaxSignal) -> str | None:
        detected_at = timestamp(signal.detected_at)
        if detected_at is None:
            return None
        return (detected_at + timedelta(seconds=INBOX_SIGNAL_MAX_AGE_SECONDS)).isoformat()

    def _plain_english(
        self,
        signal: ParallaxSignal,
        buy_play: Any | None,
    ) -> dict[str, str]:
        side = signal.side.value
        happened = (
            f"{side}-side liquidity jumped from {signal.formatted_previous_value} "
            f"to {signal.formatted_current_value} in {signal.formatted_window}."
            if signal.signal_type == SignalType.LIQUIDITY_MOVE
            else f"{side} moved from {signal.formatted_previous_value} to "
            f"{signal.formatted_current_value} in {signal.formatted_window}."
            if signal.signal_type == SignalType.PRICE_MOVE
            else f"The {side} price gap changed from {signal.formatted_previous_value} "
            f"to {signal.formatted_current_value} in {signal.formatted_window}."
        )
        if buy_play is not None:
            means = (
                f"PARALLAX has a validated BUY {buy_play.side.value} play for this "
                "market after its value, cost, freshness, spread, liquidity, "
                "confidence and risk checks."
            )
            not_mean = "This still does not guarantee the contract will finish correct."
            instruction = (
                f"This is an actionable PARALLAX Play for BUY {buy_play.side.value}. "
                "Review the risks and invalidation conditions before acting manually."
            )
            return {
                "what_happened": happened,
                "what_it_means": means,
                "what_it_does_not_mean": not_mean,
                "operator_instruction": instruction,
                "what_would_make_it_actionable": "It is already actionable while the PARALLAX Play remains fresh.",
            }
        if signal.signal_type == SignalType.LIQUIDITY_MOVE:
            means = f"More trading capacity appeared on the {side} side of the market."
            not_mean = (
                f"This does not mean PARALLAX believes {side} is more likely to win."
            )
            actionable = (
                "Validated directional price/value evidence or a PARALLAX BUY play."
            )
            instruction = (
                "Do not buy based on this liquidity change alone. Watch for price "
                "movement or an independently validated PARALLAX Play."
            )
        elif signal.signal_type == SignalType.PRICE_MOVE:
            means = "The market price changed quickly, which can be useful context but is not a value estimate."
            not_mean = "This does not mean PARALLAX has found a profitable BUY."
            actionable = "A validated PARALLAX BUY play that clears value, cost, spread, liquidity, freshness, confidence and risk gates."
            instruction = "Treat this as a watch item unless a PARALLAX BUY play appears."
        else:
            means = "The gap between the sell and buy prices changed, which affects trading cost."
            not_mean = "This does not mean either side is more likely to win."
            actionable = "A validated PARALLAX BUY play with a current executable price and acceptable trading cost."
            instruction = "Do not buy based on spread movement alone."
        return {
            "what_happened": happened,
            "what_it_means": means,
            "what_it_does_not_mean": not_mean,
            "operator_instruction": instruction,
            "what_would_make_it_actionable": actionable,
        }

    def _explained_signal(
        self,
        signal: ParallaxSignal,
        market: NormalizedMarket | None,
        buy_play: Any | None,
        now: datetime,
    ) -> dict[str, Any]:
        public_reasons = self._publishability_reasons(signal, market, now)
        side = buy_play.side if buy_play is not None else signal.side
        price = buy_play.executable_price if buy_play is not None else None
        if price is None and market is not None:
            price = market.yes_ask if side == Side.YES else market.no_ask
        actionable = buy_play is not None
        verdict = f"BUY {side.value}" if actionable else "WATCH"
        actionability = "ACTIONABLE" if actionable else "NOT_ACTIONABLE"
        directional_read = side.value if actionable else "NEUTRAL"
        confidence = (
            buy_play.confidence_band.value
            if buy_play is not None
            and buy_play.confidence_band in (Confidence.HIGH, Confidence.ELITE)
            else "NONE"
        )
        plain = self._plain_english(signal, buy_play)
        economics_label = (
            "PARALLAX PLAY ECONOMICS"
            if actionable
            else "REFERENCE ECONOMICS - NOT A RECOMMENDATION"
        )
        interpretation = {
            "verdict": verdict,
            "actionability": actionability,
            "directional_read": directional_read,
            "headline": f"PARALLAX {verdict}" if actionable else "PARALLAX WATCH",
            **plain,
            "current_market": self._current_market_snapshot(signal, market, now),
            "economics": _economics(price, economics_label),
            "risks": (
                buy_play.risk_factors
                if buy_play is not None
                else (
                    "Liquidity, price and spread can change quickly.",
                    "A market can be noisy while it is newly opened.",
                    "You can lose the full amount spent if a manual trade is wrong.",
                )
            ),
            "operator_instruction": plain["operator_instruction"],
            "public_worthy": not public_reasons,
            "public_worthy_reason": "HIGH_VALIDATED_SIGNAL"
            if not public_reasons
            else public_reasons,
            "market_activity_strength": signal.signal_strength,
            "trade_confidence": confidence,
            "parallax_play_id": None if buy_play is None else buy_play.id,
        }
        if buy_play is not None:
            interpretation["why_parallax_favors_side"] = buy_play.reason_summary
            interpretation["evidence_supporting_it"] = buy_play.reason_factors
            interpretation["what_could_make_it_wrong"] = buy_play.invalidation_conditions
        else:
            interpretation["what_could_make_it_wrong"] = (
                "The liquidity change may reverse.",
                "Other market participants may update prices for reasons PARALLAX has not validated.",
                "New information about the event can change the market.",
            )
        return {**signal.as_dict(), "retail_interpretation": interpretation}

    def explained_signals(self, plan: Plan = Plan.EXPLORER):
        with self.lock:
            rows = list(self.signal_history)
            markets = {
                (market.venue, market.venue_market_id): market for market in self.markets
            }
        rank = {SignalSignificance.HIGH: 1, SignalSignificance.MATERIAL: 0}
        rows.sort(key=lambda row: (rank[row.significance], row.detected_at), reverse=True)
        limit = min(5, entitlement(plan).play_limit)
        if entitlement(plan).permits(Feature.DETAILS):
            limit = entitlement(plan).play_limit
        plays = self._plays()
        buy_plays = {
            (play.venue, play.market_id, play.side): play
            for play in plays
            if play.suggested_action == Action.BUY
        }
        now = utcnow()
        return {
            "mode": self.mode,
            "total": len(rows),
            "limit": limit,
            "items": [
                self._explained_signal(
                    row,
                    markets.get((row.venue, row.market_id)),
                    buy_plays.get((row.venue, row.market_id, row.side)),
                    now,
                )
                for row in rows[:limit]
            ],
            "as_of": now.isoformat(),
        }

    def _inbox_rows(self, now: datetime) -> tuple[list[InboxUpsert], int]:
        with self.lock:
            rows = list(self.signal_history)
            markets = {
                (market.venue, market.venue_market_id): market for market in self.markets
            }
        rows.sort(
            key=lambda row: (
                SIGNAL_PRIORITY[row.signal_type],
                -row.absolute_change,
                row.detected_at,
            )
        )
        plays = self._plays()
        buy_plays = {
            (play.venue, play.market_id, play.side): play
            for play in plays
            if play.suggested_action == Action.BUY
        }
        by_market: dict[tuple[Venue, str], tuple[AttentionClass, InboxUpsert]] = {}
        suppressed = 0
        for signal in rows:
            market = markets.get((signal.venue, signal.market_id))
            explained = self._explained_signal(
                signal,
                market,
                buy_plays.get((signal.venue, signal.market_id, signal.side)),
                now,
            )
            attention_class = self._attention_class(explained, signal, market, now)
            if attention_class is None:
                suppressed += 1
                continue
            item = self._inbox_upsert(attention_class, explained, signal)
            key = (signal.venue, signal.market_id)
            existing = by_market.get(key)
            if existing is None or self._prefer_inbox_item(item, existing[1]):
                by_market[key] = (attention_class, item)
            else:
                suppressed += 1
        ordered = sorted(
            (row for _, row in by_market.values()),
            key=lambda row: (
                ATTENTION_PRIORITY[row.attention_class],
                -_iso_timestamp(row.detected_at),
            ),
        )
        return ordered[:INBOX_ACTIVE_LIMIT], suppressed + max(0, len(ordered) - INBOX_ACTIVE_LIMIT)

    def _attention_class(
        self,
        explained: dict[str, Any],
        signal: ParallaxSignal,
        market: NormalizedMarket | None,
        now: datetime,
    ) -> AttentionClass | None:
        retail = explained["retail_interpretation"]
        if (
            retail["actionability"] == "ACTIONABLE"
            and retail["verdict"] in {"BUY YES", "BUY NO"}
        ):
            return AttentionClass.ACTIONABLE_PLAY
        if retail["public_worthy"] is True:
            return AttentionClass.PUBLIC_WORTHY
        if (
            retail["verdict"] == "WATCH"
            and retail["actionability"] != "ACTIONABLE"
            and retail["market_activity_strength"] == SignalSignificance.HIGH.value
            and signal.signal_type in {SignalType.PRICE_MOVE, SignalType.SPREAD_MOVE}
            and self._priority_watch_reasons(signal, market, now) == ()
        ):
            return AttentionClass.PRIORITY_WATCH
        return None

    def _priority_watch_reasons(
        self,
        signal: ParallaxSignal,
        market: NormalizedMarket | None,
        now: datetime,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if signal.significance != SignalSignificance.HIGH:
            reasons.append("NOT_HIGH_MARKET_ACTIVITY")
        if signal.signal_type not in {SignalType.PRICE_MOVE, SignalType.SPREAD_MOVE}:
            reasons.append("UNSUPPORTED_SIGNAL_TYPE")
        detected_at = timestamp(signal.detected_at)
        cutoff = now - timedelta(seconds=INBOX_SIGNAL_MAX_AGE_SECONDS)
        if detected_at is None or detected_at < cutoff:
            reasons.append("STALE_SIGNAL")
        resolved_at = timestamp(signal.resolution_time)
        if resolved_at is not None and resolved_at <= now:
            reasons.append("RESOLVED_OR_EXPIRED_MARKET")
        if market is None:
            reasons.append("MISSING_RETAIL_CONTEXT")
        else:
            if market.status.upper() in {"CLOSED", "RESOLVED", "EXPIRED"}:
                reasons.append("RESOLVED_OR_EXPIRED_MARKET")
            age = _market_age_seconds(market, now)
            if (
                age is not None
                and age < NEW_MARKET_PUBLICATION_BLOCK_SECONDS
                and signal.signal_type in TEMPORAL_SIGNAL_TYPES
            ):
                reasons.append("NEW_MARKET_PRICE_DISCOVERY")
            if not _valid_two_sided_book(market.yes_bid, market.yes_ask):
                reasons.append("INVALID_YES_BOOK")
            if not _valid_two_sided_book(market.no_bid, market.no_ask):
                reasons.append("INVALID_NO_BOOK")
        if not _has_publishable_context(signal):
            reasons.append("MISSING_RETAIL_CONTEXT")
        return tuple(dict.fromkeys(reasons))

    def _inbox_upsert(
        self,
        attention_class: AttentionClass,
        explained: dict[str, Any],
        signal: ParallaxSignal,
    ) -> InboxUpsert:
        retail = explained["retail_interpretation"]
        payload = {
            "headline": retail["headline"],
            "market": explained["display_title"] or explained["market_title"],
            "what_happened": retail["what_happened"],
            "what_it_means": retail["what_it_means"],
            "operator_instruction": retail["operator_instruction"],
            "why": retail.get("why_parallax_favors_side"),
            "economics": retail["economics"],
            "risks": retail["risks"],
            "invalidation": retail.get("what_could_make_it_wrong"),
            "current_market": retail["current_market"],
            "parallax_play_id": retail.get("parallax_play_id"),
            "source_signal": {
                "signal_type": signal.signal_type.value,
                "side": signal.side.value,
                "formatted_previous_value": signal.formatted_previous_value,
                "formatted_current_value": signal.formatted_current_value,
                "formatted_change": signal.formatted_change,
                "formatted_window": signal.formatted_window,
            },
        }
        return InboxUpsert(
            attention_class=attention_class,
            source_id=signal.id,
            venue=signal.venue.value,
            market_id=signal.market_id,
            display_title=explained["display_title"] or explained["market_title"],
            headline=retail["headline"],
            verdict=retail["verdict"],
            actionability=retail["actionability"],
            directional_read=retail["directional_read"],
            trade_confidence=retail["trade_confidence"],
            market_activity_strength=retail["market_activity_strength"],
            what_happened=retail["what_happened"],
            what_it_means=retail["what_it_means"],
            operator_instruction=retail["operator_instruction"],
            detected_at=signal.detected_at,
            expires_at=self._inbox_expiry(signal),
            payload=payload,
        )

    @staticmethod
    def _prefer_inbox_item(candidate: InboxUpsert, existing: InboxUpsert) -> bool:
        candidate_priority = ATTENTION_PRIORITY[candidate.attention_class]
        existing_priority = ATTENTION_PRIORITY[existing.attention_class]
        if candidate_priority != existing_priority:
            return candidate_priority < existing_priority
        return _iso_timestamp(candidate.detected_at) > _iso_timestamp(existing.detected_at)

    def refresh_inbox(self) -> None:
        now = utcnow()
        items, suppressed = self._inbox_rows(now)
        for item in items:
            self.inbox_store.upsert(item)
        active_ids = {
            self.inbox_store.inbox_id(item.venue, item.market_id, item.attention_class)
            for item in items
        }
        self.inbox_store.expire_missing_active(active_ids)
        self.inbox_suppressed = suppressed
        self.alert_dispatcher.dispatch(self.inbox_store.items())
        if self.mode != "demo":
            for stored_item in self.inbox_store.items():
                if stored_item.get("status") == InboxStatus.ACTIVE.value:
                    self.social.enqueue(stored_item)
            self.social.publish_pending()

    def inbox(self, *, include_expired: bool = False) -> dict[str, Any]:
        self.refresh_inbox()
        items = self.inbox_store.items(include_expired=include_expired)
        active = [item for item in items if item["status"] == InboxStatus.ACTIVE.value]
        summary = {
            "actionable_plays": sum(
                item["attention_class"] == AttentionClass.ACTIONABLE_PLAY.value
                for item in active
            ),
            "public_worthy": sum(
                item["attention_class"] == AttentionClass.PUBLIC_WORTHY.value
                for item in active
            ),
            "priority_watch": sum(
                item["attention_class"] == AttentionClass.PRIORITY_WATCH.value
                for item in active
            ),
            "unseen": sum(not item["seen"] for item in active),
            "suppressed": self.inbox_suppressed,
        }
        return {
            "mode": self.mode,
            "summary": summary,
            "total": len(items),
            "limit": INBOX_ACTIVE_LIMIT,
            "items": items,
            "as_of": utcnow().isoformat(),
        }

    def mark_inbox_seen(self, inbox_id: str) -> dict[str, Any]:
        return self.inbox_store.mark_seen(inbox_id)

    def alerts_status(self) -> dict[str, Any]:
        return self.alert_dispatcher.status()

    def alerts_recent(self, *, limit: int = 20) -> dict[str, Any]:
        items = self.alert_dispatcher.recent(limit)
        return {
            "mode": self.alert_dispatcher.config.mode,
            "total": len(items),
            "limit": limit,
            "items": items,
            "as_of": utcnow().isoformat(),
        }

    def social_status(self) -> dict[str, Any]:
        return self.social.status()

    def social_outbox(self, *, limit: int = 20) -> dict[str, Any]:
        return {"items": self.social.store.outbox(limit), "limit": min(max(limit, 1), 100)}

    def social_publications(self, *, limit: int = 20) -> dict[str, Any]:
        return {"items": self.social.store.publications(limit), "limit": min(max(limit, 1), 100)}

    def _publishable_rows(
        self,
        plan: Plan = Plan.EXPLORER,
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        now = now or utcnow()
        with self.lock:
            markets = {
                (market.venue, market.venue_market_id): market for market in self.markets
            }
            rows = list(self.signal_history)
        publishable: list[ParallaxSignal] = []
        seen_markets: set[tuple[Venue, str]] = set()
        rows.sort(
            key=lambda row: (
                SIGNAL_PRIORITY[row.signal_type],
                -row.absolute_change,
                row.detected_at,
            )
        )
        for signal in rows:
            market = markets.get((signal.venue, signal.market_id))
            if self._publishability_reasons(signal, market, now):
                continue
            market_key = (signal.venue, signal.market_id)
            if market_key in seen_markets:
                continue
            seen_markets.add(market_key)
            publishable.append(signal)
        publishable.sort(
            key=lambda row: (
                SIGNAL_PRIORITY[row.signal_type],
                -row.absolute_change,
                row.detected_at,
            )
        )
        limit = (
            PUBLISHABLE_SIGNAL_LIMIT
            if entitlement(plan).permits(Feature.DETAILS)
            else EXPLORER_PUBLISHABLE_SIGNAL_LIMIT
        )
        items = []
        for signal in publishable[:limit]:
            detected_at = timestamp(signal.detected_at)
            publishable_until = (
                detected_at + timedelta(seconds=PUBLISHABLE_SIGNAL_MAX_AGE_SECONDS)
                if detected_at
                else None
            )
            items.append(
                {
                **signal.as_dict(),
                "publishable": True,
                "publishability_reason": "HIGH_VALIDATED_SIGNAL",
                "publishable_until": publishable_until.isoformat()
                if publishable_until
                else None,
                "social_preview": _social_preview(signal),
                }
            )
        return items

    def publishable_signals(self, plan: Plan = Plan.EXPLORER):
        items = self._publishable_rows(plan)
        return {
            "mode": self.mode,
            "total": len(items),
            "limit": (
                PUBLISHABLE_SIGNAL_LIMIT
                if entitlement(plan).permits(Feature.DETAILS)
                else EXPLORER_PUBLISHABLE_SIGNAL_LIMIT
            ),
            "items": items,
            "as_of": utcnow().isoformat(),
        }

    def health(self):
        plays = self._plays()
        self.refresh_inbox()
        inbox_items = self.inbox_store.items()
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
                "publishable_signals": len(self._publishable_rows(Plan.PRO)),
                "inbox_active": len(inbox_items),
                "inbox_unseen": sum(not item["seen"] for item in inbox_items),
                "inbox_actionable": sum(
                    item["attention_class"] == AttentionClass.ACTIONABLE_PLAY.value
                    for item in inbox_items
                ),
                "inbox_public_worthy": sum(
                    item["attention_class"] == AttentionClass.PUBLIC_WORTHY.value
                    for item in inbox_items
                ),
                "inbox_priority_watch": sum(
                    item["attention_class"] == AttentionClass.PRIORITY_WATCH.value
                    for item in inbox_items
                ),
            }
        )
        metrics = dict(metrics)
        alert_status = self.alerts_status()
        social_status = self.social_status()
        metrics.update(
            {
                "alerts_pending": alert_status["pending"],
                "alerts_sent": alert_status["sent"],
                "alerts_failed": alert_status["failed"],
                "alerts_unknown": alert_status["unknown"],
                "social_mode": social_status["mode"],
                "social_pending": social_status["pending"],
                "social_dry_run": social_status["dry_run"],
                "social_sent": social_status["sent"],
                "social_failed": social_status["failed"],
                "social_unknown": social_status["unknown"],
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
            "alert_mode": alert_status["mode"],
            "last_alert_delivery_at": alert_status["last_delivery_at"],
            "social": social_status,
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


def _iso_timestamp(value: str) -> float:
    parsed = timestamp(value)
    return 0.0 if parsed is None else parsed.timestamp()
