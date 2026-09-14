"""Exact venue-contract binding for externally supplied forecasts.

This module does not reconstruct abstract events and does not forecast.  A caller
must freeze a methodology against one already inspected venue contract, including
its rules, outcome labels, and stable identifier, before the forecast can bind.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .engine import qualify
from .generic_events import normalize_binary_event
from .models import Evidence, NormalizedMarket, PlayType, Side, Venue, timestamp, utcnow
from .normalization import rules_digest as market_rules_digest

if TYPE_CHECKING:
    from .event_discovery import EventCandidate


class DirectBindingStatus(StrEnum):
    EXACT_CONTRACT = "EXACT_CONTRACT"
    MISMATCH = "MISMATCH"
    STALE = "STALE"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class DirectForecastFreshnessPolicy:
    max_age: timedelta = timedelta(seconds=60)

    def validate(self) -> DirectForecastFreshnessPolicy:
        seconds = self.max_age.total_seconds()
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Direct forecast freshness must be positive and finite")
        return self


def direct_contract_fingerprint(market: NormalizedMarket) -> str:
    """Hash the exact contract identity and resolution-relevant immutable fields."""
    payload = {
        "venue": market.venue.value,
        "contract_id": market.venue_market_id,
        "slug": market.slug,
        "canonical_question": market.title,
        "outcomes": market.outcomes,
        "rules_digest": market_rules_digest(market),
        "resolution_time": market.resolution_time,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class DirectContractForecast:
    venue: Venue
    contract_id: str
    canonical_question: str
    outcome: str
    probability: float
    expected_outcome: str
    confidence: str
    forecasted_at: str
    method: str
    method_version: str
    contract_fingerprint: str
    rules_digest: str
    resolution_reference: str
    evidence_references: tuple[str, ...]
    assumptions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    external_methodology_metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> DirectContractForecast:
        required = {
            "contract_id": self.contract_id,
            "canonical_question": self.canonical_question,
            "outcome": self.outcome,
            "expected_outcome": self.expected_outcome,
            "confidence": self.confidence,
            "forecasted_at": self.forecasted_at,
            "method": self.method,
            "method_version": self.method_version,
            "contract_fingerprint": self.contract_fingerprint,
            "rules_digest": self.rules_digest,
            "resolution_reference": self.resolution_reference,
        }
        if any(not str(value or "").strip() for value in required.values()):
            raise ValueError("Direct forecast requires exact identity, method, and resolution fields")
        if not isinstance(self.venue, Venue):
            raise TypeError("Direct forecast requires a supported venue")
        if isinstance(self.probability, bool) or not isinstance(self.probability, (int, float)):
            raise TypeError("Direct forecast probability must be numeric")
        if not math.isfinite(float(self.probability)):
            raise ValueError("Direct forecast probability must be finite")
        if not 0.0 <= float(self.probability) <= 1.0:
            raise ValueError("Direct forecast probability must be within [0, 1]")
        if timestamp(self.forecasted_at) is None:
            raise ValueError("Direct forecast timestamp must be timezone-aware ISO-8601")
        if not self.evidence_references or any(not str(ref).strip() for ref in self.evidence_references):
            raise ValueError("Direct forecast requires preserved evidence references")
        return self

    def as_metadata(self) -> dict[str, Any]:
        self.validate()
        metadata = asdict(self)
        metadata["venue"] = self.venue.value
        metadata["probability"] = float(self.probability)
        metadata["binding_type"] = "DIRECT_CONTRACT"
        metadata["externally_supplied"] = True
        metadata["execution_claim"] = False
        metadata["publication_claim"] = False
        return metadata


@dataclass(frozen=True)
class DirectContractBinding:
    status: DirectBindingStatus
    reason: str
    evidence: Evidence | None = None
    forecast: DirectContractForecast | None = None
    side: Side | None = None


@dataclass(frozen=True)
class DirectContractEvaluation:
    status: str
    reason: str
    play: Any | None = None
    binding: DirectContractBinding | None = None


def bind_direct_contract_forecast(
    forecast: DirectContractForecast,
    market: NormalizedMarket,
    *,
    now: datetime | None = None,
    freshness: DirectForecastFreshnessPolicy | None = None,
) -> DirectContractBinding:
    """Create Evidence only for the exact, open contract named by the methodology."""
    try:
        forecast.validate()
        policy = (freshness or DirectForecastFreshnessPolicy()).validate()
    except (TypeError, ValueError) as exc:
        return DirectContractBinding(DirectBindingStatus.UNSUPPORTED, str(exc))

    exact_checks = (
        (forecast.venue == market.venue, "venue differs"),
        (forecast.contract_id == market.venue_market_id, "stable contract identifier differs"),
        (forecast.canonical_question == market.title, "canonical question differs"),
        (forecast.rules_digest == market_rules_digest(market), "resolution rules digest differs"),
        (forecast.contract_fingerprint == direct_contract_fingerprint(market), "contract fingerprint differs"),
    )
    for matches, reason in exact_checks:
        if not matches:
            return DirectContractBinding(DirectBindingStatus.MISMATCH, reason, forecast=forecast)

    if set(market.outcomes) != {Side.YES.value, Side.NO.value}:
        return DirectContractBinding(
            DirectBindingStatus.UNSUPPORTED,
            "contract does not expose exactly one YES and one NO side",
            forecast=forecast,
        )
    matching_sides = tuple(
        Side(side) for side, label in market.outcomes.items() if label == forecast.outcome
    )
    if len(matching_sides) != 1:
        return DirectContractBinding(
            DirectBindingStatus.MISMATCH,
            "forecast outcome does not exactly match one binary outcome label",
            forecast=forecast,
        )

    observed = (now or utcnow()).astimezone(UTC)
    forecast_at = timestamp(forecast.forecasted_at)
    assert forecast_at is not None
    if forecast_at > observed or observed - forecast_at > policy.max_age:
        return DirectContractBinding(
            DirectBindingStatus.STALE, "forecast is outside the explicit freshness window", forecast=forecast
        )

    if market.status != "OPEN":
        return DirectContractBinding(
            DirectBindingStatus.UNSUPPORTED, "contract is not open", forecast=forecast
        )
    if market.resolution_time:
        resolution = timestamp(market.resolution_time)
        if resolution is None or resolution <= observed:
            return DirectContractBinding(
                DirectBindingStatus.UNSUPPORTED,
                "contract resolution deadline is invalid or has passed",
                forecast=forecast,
            )

    side = matching_sides[0]
    yes_probability = float(forecast.probability) if side is Side.YES else 1.0 - float(forecast.probability)
    valid_until = forecast_at + policy.max_age
    evidence = Evidence(
        venue=market.venue,
        market_id=market.venue_market_id,
        fair_probability=yes_probability,
        source=f"direct-contract:{forecast.method}",
        model_version=forecast.method_version,
        observed_at=forecast_at.isoformat(),
        valid_until=valid_until.isoformat(),
        rules_digest=forecast.rules_digest,
        review_reference=forecast.resolution_reference,
        rationale=(
            "Externally supplied direct-contract forecast; assumptions: "
            + "; ".join(forecast.assumptions)
            + "; limitations: "
            + "; ".join(forecast.limitations)
        ),
        independent_sources=forecast.evidence_references,
        validation_reference=f"external methodology {forecast.method_version}",
        play_type=PlayType.PARALLAX_EDGE,
        source_independence="EVENT_SPECIFIC",
        validation_status="EXPERIMENTAL",
        forecast_metadata=forecast.as_metadata(),
    )
    return DirectContractBinding(
        DirectBindingStatus.EXACT_CONTRACT,
        "all immutable contract identity fields match",
        evidence,
        forecast,
        side,
    )


def evaluate_direct_contract_forecast(
    market: NormalizedMarket,
    forecast: DirectContractForecast,
    *,
    now: datetime | None = None,
    freshness: DirectForecastFreshnessPolicy | None = None,
) -> DirectContractEvaluation:
    binding = bind_direct_contract_forecast(forecast, market, now=now, freshness=freshness)
    if binding.status is not DirectBindingStatus.EXACT_CONTRACT or binding.evidence is None:
        return DirectContractEvaluation(binding.status.value, binding.reason, binding=binding)
    play = qualify(market, binding.side or Side.YES, binding.evidence, now=now)
    status = "QUALIFIED" if play.suggested_action.value == "BUY" else play.suggested_action.value
    return DirectContractEvaluation(status, "existing qualification machinery", play, binding)


def capture_direct_contract_forecast(
    store: Any,
    market: NormalizedMarket,
    forecast: DirectContractForecast,
    *,
    now: datetime | None = None,
    freshness: DirectForecastFreshnessPolicy | None = None,
) -> dict[str, Any]:
    """Freeze an exact direct-contract evaluation in the existing immutable ledger."""
    binding = bind_direct_contract_forecast(forecast, market, now=now, freshness=freshness)
    if (
        binding.status is not DirectBindingStatus.EXACT_CONTRACT
        or binding.evidence is None
        or binding.side is None
    ):
        raise ValueError(f"Cannot capture direct forecast: {binding.status.value} ({binding.reason})")
    return store.capture_prospective(market, binding.side, binding.evidence, now=now)


def normalized_for_direct_contract(candidate: EventCandidate) -> NormalizedMarket:
    """Adapt one discovered contract without requiring an abstract-event match."""
    raw = dict(candidate.raw_provenance.get("market") or {})
    raw["question"] = candidate.contract_question
    raw["description"] = candidate.resolution_text
    if candidate.venue is Venue.POLYMARKET:
        raw["id"] = candidate.market_id
        raw["slug"] = candidate.slug or candidate.market_id
        raw["marketSides"] = [
            {"long": True, "description": candidate.outcomes["YES"]},
            {"long": False, "description": candidate.outcomes["NO"]},
        ]
    else:
        raw["ticker"] = candidate.market_id
        raw["title"] = candidate.contract_question
        raw["rules_primary"] = candidate.resolution_text
        raw["yes_sub_title"] = candidate.outcomes["YES"]
        raw["no_sub_title"] = candidate.outcomes["NO"]
    if candidate.resolution_time:
        raw["resolution_time"] = candidate.resolution_time
    reference = candidate.source_reference or f"inventory://{candidate.venue.value}/{candidate.market_id}"
    return normalize_binary_event(
        raw,
        venue=candidate.venue,
        event_id=f"direct-contract:{candidate.venue.value}:{candidate.market_id}",
        event_family=candidate.event_family.value,
        event_question=candidate.contract_question,
        resolution_reference=reference,
        book=candidate.raw_provenance.get("book") or {},
        event=candidate.raw_provenance.get("event") or {},
        observed_at=candidate.source_timestamp or candidate.discovery_timestamp,
    )


__all__ = [
    "DirectBindingStatus",
    "DirectContractBinding",
    "DirectContractEvaluation",
    "DirectContractForecast",
    "DirectForecastFreshnessPolicy",
    "bind_direct_contract_forecast",
    "capture_direct_contract_forecast",
    "direct_contract_fingerprint",
    "evaluate_direct_contract_forecast",
    "normalized_for_direct_contract",
]
