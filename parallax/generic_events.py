"""Small reusable adapter for explicitly supplied non-sports event forecasts.

This module does not forecast.  It validates an externally supplied forecast,
requires exact event/contract identity, and adapts it to PARALLAX Evidence.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from .engine import qualify
from .models import (
    Evidence,
    Mechanics,
    NormalizedMarket,
    PlayType,
    Side,
    Venue,
    timestamp,
    utcnow,
)
from .normalization import rules_digest


class EventFamily(StrEnum):
    LEGISLATIVE_REGULATORY = "LEGISLATIVE_REGULATORY"
    MACRO_MONETARY = "MACRO_MONETARY"
    ELECTION_POLITICAL = "ELECTION_POLITICAL"
    LEGAL_JUDICIAL = "LEGAL_JUDICIAL"
    CORPORATE = "CORPORATE"
    GEOPOLITICAL = "GEOPOLITICAL"
    OTHER_BINARY_EVENT = "OTHER_BINARY_EVENT"


EVENT_FAMILIES = frozenset(item.value for item in EventFamily)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


@dataclass(frozen=True)
class EventForecast:
    event_id: str
    event_family: str
    event_question: str
    forecasted_outcome: str
    probability: float
    method_name: str
    method_version: str
    confidence: str
    evidence_references: tuple[str, ...]
    assumptions: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    forecasted_at: str
    authoritative_resolution: str
    authoritative_resolution_reference: str
    expected_outcome: str | None = None
    notes: str | None = None

    def validate(self) -> EventForecast:
        required = {
            "event_id": self.event_id, "event_family": self.event_family,
            "event_question": self.event_question, "forecasted_outcome": self.forecasted_outcome,
            "method_name": self.method_name, "method_version": self.method_version,
            "confidence": self.confidence, "forecasted_at": self.forecasted_at,
            "authoritative_resolution": self.authoritative_resolution,
            "authoritative_resolution_reference": self.authoritative_resolution_reference,
        }
        if any(not _clean(value) for value in required.values()):
            raise ValueError("Event forecast requires all identity, method, confidence, and resolution fields")
        if self.event_family not in EVENT_FAMILIES:
            raise ValueError("Unsupported event family")
        if not isinstance(self.probability, (int, float)) or not math.isfinite(float(self.probability)):
            raise ValueError("Event forecast probability must be finite")
        if not 0.0 < float(self.probability) < 1.0:
            raise ValueError("Event forecast probability must be strictly between 0 and 1")
        if timestamp(self.forecasted_at) is None:
            raise ValueError("Event forecast timestamp must be timezone-aware ISO-8601")
        if not self.evidence_references or any(not _clean(ref) for ref in self.evidence_references):
            raise ValueError("Event forecast requires at least one evidence reference")
        return self

    def as_metadata(self) -> dict[str, Any]:
        self.validate()
        return {
            "event_id": self.event_id,
            "event_family": self.event_family,
            "event_question": self.event_question,
            "forecasted_outcome": self.forecasted_outcome,
            "probability": float(self.probability),
            "method_name": self.method_name,
            "method_version": self.method_version,
            "confidence": self.confidence,
            "evidence_references": list(self.evidence_references),
            "assumptions": list(self.assumptions),
            "invalidation_conditions": list(self.invalidation_conditions),
            "forecasted_at": self.forecasted_at,
            "authoritative_resolution": self.authoritative_resolution,
            "authoritative_resolution_reference": self.authoritative_resolution_reference,
            "expected_outcome": self.expected_outcome,
            "notes": self.notes,
        }


class EventForecastProvider(Protocol):
    def supports(self, event_id: str, event_family: str) -> bool: ...
    def forecast(self, event_id: str, event_family: str) -> EventForecast: ...


class StaticEventForecastProvider:
    """Explicit input provider for bounded experiments; never infers forecasts."""

    def __init__(self, forecasts: Mapping[str, EventForecast]):
        self.forecasts = dict(forecasts)

    def supports(self, event_id: str, event_family: str) -> bool:
        forecast = self.forecasts.get(event_id)
        return bool(forecast and forecast.event_family == event_family)

    def forecast(self, event_id: str, event_family: str) -> EventForecast:
        if not self.supports(event_id, event_family):
            raise ValueError("No exact event forecast input")
        return self.forecasts[event_id].validate()


@dataclass(frozen=True)
class EventForecastBinding:
    status: str
    reason: str
    evidence: Evidence | None = None
    forecast: EventForecast | None = None
    side: str | None = None


@dataclass(frozen=True)
class EventEvaluation:
    status: str
    reason: str
    play: Any | None = None
    binding: EventForecastBinding | None = None


def _identity(market: NormalizedMarket) -> dict[str, str] | None:
    raw = market.original_metadata.get("event_identity")
    if not isinstance(raw, dict):
        market_raw = market.original_metadata.get("market")
        raw = market_raw.get("event_identity") if isinstance(market_raw, dict) else None
    if not isinstance(raw, dict):
        return None
    fields = {key: _clean(raw.get(key)) for key in ("event_id", "event_family", "event_question", "resolution_reference")}
    return fields if all(fields.values()) else None


def bind_forecast(forecast: EventForecast, market: NormalizedMarket, *, now: datetime | None = None) -> EventForecastBinding:
    """Bind only when event identity and selected outcome match exactly."""
    try:
        forecast.validate()
    except ValueError as exc:
        return EventForecastBinding("INVALID_FORECAST", str(exc))
    identity = _identity(market)
    expected = {
        "event_id": forecast.event_id,
        "event_family": forecast.event_family,
        "event_question": forecast.event_question,
        "resolution_reference": forecast.authoritative_resolution_reference,
    }
    if identity != expected:
        return EventForecastBinding("NO_EXACT_CONTRACT", "market event identity does not exactly match forecast")
    labels = {str(value).strip().casefold(): side for side, value in market.outcomes.items() if str(value).strip()}
    outcome_key = _clean(forecast.forecasted_outcome).casefold()
    if outcome_key not in labels or len(labels) != 2:
        return EventForecastBinding("NO_EXACT_CONTRACT", "forecast outcome is not one of exactly two market outcomes")
    observed = (now or utcnow()).astimezone(UTC)
    forecast_at = timestamp(forecast.forecasted_at)
    if forecast_at is None or forecast_at > observed:
        return EventForecastBinding("INVALID_FORECAST", "forecast timestamp is invalid or in the future")
    side = labels[outcome_key]
    yes_probability = forecast.probability if side == "YES" else 1.0 - forecast.probability
    metadata = forecast.as_metadata()
    evidence = Evidence(
        venue=market.venue,
        market_id=market.venue_market_id,
        fair_probability=yes_probability,
        source=f"event-forecast:{forecast.method_name}",
        model_version=forecast.method_version,
        observed_at=forecast.forecasted_at,
        valid_until=(observed + timedelta(seconds=60)).isoformat(),
        rules_digest=rules_digest(market),
        review_reference=forecast.authoritative_resolution_reference,
        rationale=(forecast.notes or forecast.method_name) + "; assumptions: " + "; ".join(forecast.assumptions),
        independent_sources=forecast.evidence_references,
        validation_reference=f"event-specific method {forecast.method_version}",
        play_type=PlayType.PARALLAX_EDGE,
        source_independence="EVENT_SPECIFIC",
        validation_status="EXPERIMENTAL",
        forecast_metadata=metadata,
    )
    return EventForecastBinding("MATCHED", "exact event and contract identity", evidence, forecast, side)


def evaluate_event_forecast(
    market: NormalizedMarket,
    forecast: EventForecast,
    *,
    now: datetime | None = None,
) -> EventEvaluation:
    binding = bind_forecast(forecast, market, now=now)
    if binding.status != "MATCHED" or binding.evidence is None:
        return EventEvaluation(binding.status, binding.reason, binding=binding)
    play = qualify(market, Side(binding.side or "YES"), binding.evidence, now=now)
    return EventEvaluation("QUALIFIED" if play.suggested_action.value == "BUY" else play.suggested_action.value, "existing qualification machinery", play, binding)


def capture_event_forecast(store: Any, market: NormalizedMarket, forecast: EventForecast, *, now: datetime | None = None) -> dict[str, Any]:
    """Capture a matched event forecast through the existing immutable ledger."""
    binding = bind_forecast(forecast, market, now=now)
    if binding.status != "MATCHED" or binding.evidence is None or binding.side is None:
        raise ValueError(f"Cannot capture event forecast: {binding.status} ({binding.reason})")
    return store.capture_prospective(market, Side(binding.side), binding.evidence, now=now)


def normalize_binary_event(
    raw: Mapping[str, Any],
    *,
    venue: Venue,
    event_id: str,
    event_family: str,
    event_question: str,
    resolution_reference: str,
    book: Mapping[str, Any] | None = None,
    event: Mapping[str, Any] | None = None,
    observed_at: str | None = None,
) -> NormalizedMarket:
    """Normalize a generic PMUS/Kalshi binary row without inventing prices."""
    source = dict(raw)
    row = dict(source.get("raw", source))
    book = book or {}
    event = event or {}
    if event_family not in EVENT_FAMILIES:
        raise ValueError("Unsupported event family")
    market_id = _clean(row.get("id") or row.get("ticker") or row.get("slug") or source.get("id") or source.get("ticker"))
    title = _clean(row.get("question") or row.get("title") or row.get("subtitle"))
    rules = _clean(row.get("rules_primary") or row.get("rules_secondary") or row.get("description"))
    if not market_id or not title or not rules:
        raise ValueError("Binary event requires market ID, title, and resolution rules")
    if venue == Venue.POLYMARKET:
        sides = row.get("marketSides")
        outcomes = {
            "YES" if side.get("long") is True else "NO": _clean(side.get("description"))
            for side in sides or [] if isinstance(side, dict) and side.get("long") in (True, False)
        }
        slug = _clean(source.get("slug") or row.get("slug") or market_id)
        yes = book.get(f"{slug}::YES", {}) if isinstance(book, Mapping) else {}
        no = book.get(f"{slug}::NO", {}) if isinstance(book, Mapping) else {}
        yes_ask, no_ask = _number(yes.get("best_ask")), _number(no.get("best_ask"))
        yes_bid, no_bid = _number(yes.get("best_bid")), _number(no.get("best_bid"))
        yes_size, no_size = _number(yes.get("ask_size_shares")) or 0, _number(no.get("ask_size_shares")) or 0
    else:
        outcomes = {"YES": _clean(row.get("yes_sub_title") or "YES"), "NO": _clean(row.get("no_sub_title") or "NO")}
        slug = market_id
        fp = book.get("orderbook_fp", {}) if isinstance(book, Mapping) else {}
        legacy = book.get("orderbook", {}) if isinstance(book, Mapping) else {}
        yes_rows = fp.get("yes_dollars", []) if isinstance(fp, dict) else legacy.get("yes", [])
        no_rows = fp.get("no_dollars", []) if isinstance(fp, dict) else legacy.get("no", [])
        yes_bid = _number(yes_rows[0][0]) if yes_rows else None
        no_bid = _number(no_rows[0][0]) if no_rows else None
        if not isinstance(fp, dict) and yes_bid is not None: yes_bid /= 100
        if not isinstance(fp, dict) and no_bid is not None: no_bid /= 100
        yes_ask, no_ask = (1 - no_bid if no_bid is not None else None), (1 - yes_bid if yes_bid is not None else None)
        yes_size = _number(no_rows[0][1]) or 0 if no_rows else 0
        no_size = _number(yes_rows[0][1]) or 0 if yes_rows else 0
    if len(outcomes) != 2 or any(not value for value in outcomes.values()) or len({v.casefold() for v in outcomes.values()}) != 2:
        raise ValueError("Binary event must have exactly two distinct outcome labels")
    depth = {
        "YES": ((yes_ask, yes_size),) if yes_ask is not None and yes_size > 0 else (),
        "NO": ((no_ask, no_size),) if no_ask is not None and no_size > 0 else (),
    }
    identity = {"event_id": event_id, "event_family": event_family, "event_question": event_question, "resolution_reference": resolution_reference}
    timestamp_value = observed_at or utcnow().isoformat()
    tick = _number(row.get("orderPriceMinTickSize") or row.get("price_tick"))
    price_ranges: tuple[tuple[float, float, float], ...] = ()
    if tick is not None and tick > 0:
        price_ranges = ((0.0, 1.0, tick),)
    elif isinstance(row.get("price_ranges"), list):
        try:
            price_ranges = tuple((float(item["start"]), float(item["end"]), float(item["step"])) for item in row["price_ranges"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Malformed binary event price ranges") from None
    payout = _number(row.get("payout") or row.get("notional_value_dollars"))
    if payout is not None and payout <= 0:
        raise ValueError("Binary event payout must be positive")
    return NormalizedMarket(
        venue=venue, venue_market_id=market_id, slug=slug, title=title,
        description=_clean(row.get("description") or row.get("subtitle")),
        category=event_family, event=_clean(row.get("event_ticker") or row.get("event_id") or event.get("id") or event_id),
        outcomes=outcomes, resolution_rules=rules,
        resolution_time=_clean(row.get("resolution_time") or row.get("expected_expiration_time") or row.get("expiration_time") or row.get("endDate")) or None,
        status="OPEN" if str(row.get("status") or "active").lower() in {"active", "open"} else str(row.get("status") or "UNKNOWN").upper(),
        yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid, no_ask=no_ask,
        best_bid_size=max(yes_size, no_size), best_ask_size=max(yes_size, no_size), executable_depth=depth,
        recent_volume=_number(row.get("volume24hr") or row.get("volume_24h") or row.get("volume")),
        recent_trade_count=None, last_trade_time=None, book_timestamp=_clean(book.get("transact_time")) or None,
        data_timestamp=timestamp_value, source_url=_clean(row.get("url")) or None,
        mechanics=Mechanics(quantity_step=1, price_ranges=price_ranges, payout=payout),
        original_metadata={"market": row, "event": dict(event), "event_identity": identity, "source_timestamp": timestamp_value},
        event_title=_clean(event.get("title") or event.get("name")) or None,
    )


__all__ = [
    "EVENT_FAMILIES", "EventEvaluation", "EventFamily", "EventForecast", "EventForecastBinding",
    "EventForecastProvider", "StaticEventForecastProvider", "bind_forecast",
    "capture_event_forecast", "evaluate_event_forecast", "normalize_binary_event",
]
