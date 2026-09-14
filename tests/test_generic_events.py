from __future__ import annotations

import math
from dataclasses import replace
from datetime import timedelta

import pytest

from parallax.generic_events import (
    EventForecast,
    StaticEventForecastProvider,
    bind_forecast,
    capture_event_forecast,
    evaluate_event_forecast,
    normalize_binary_event,
)
from parallax.models import Venue, utcnow
from parallax.track_record import TrackRecord


def forecast(**changes):
    value = EventForecast(
        event_id="event-1",
        event_family="OTHER_BINARY_EVENT",
        event_question="Will the scheduled binary event resolve YES?",
        forecasted_outcome="YES",
        probability=0.63,
        method_name="bounded-input",
        method_version="bounded-input-v1",
        confidence="MEDIUM_LOW",
        evidence_references=("fixture://evidence-1",),
        assumptions=("fixture assumption",),
        invalidation_conditions=("event identity changes",),
        forecasted_at=(utcnow() - timedelta(seconds=1)).isoformat(),
        authoritative_resolution="official binary result",
        authoritative_resolution_reference="official://event-1",
        expected_outcome="YES",
        notes="externally supplied fixture",
    )
    return replace(value, **changes)


def raw_event():
    return {
        "id": "market-1",
        "slug": "market-1",
        "question": "Will the scheduled binary event resolve YES?",
        "description": "Official binary result rules.",
        "category": "events",
        "status": "active",
        "orderPriceMinTickSize": 0.01,
        "payout": 1.0,
        "marketSides": [
            {"long": True, "description": "YES"},
            {"long": False, "description": "NO"},
        ],
        "event_identity": {
            "event_id": "event-1",
            "event_family": "OTHER_BINARY_EVENT",
            "event_question": "Will the scheduled binary event resolve YES?",
            "resolution_reference": "official://event-1",
        },
    }


def market(now=None, *, book=True):
    now = now or utcnow()
    book_data = {
        "market-1::YES": {"best_bid": 0.55, "best_ask": 0.58, "ask_size_shares": 100, "bid_size_shares": 100},
        "market-1::NO": {"best_bid": 0.40, "best_ask": 0.43, "ask_size_shares": 100, "bid_size_shares": 100},
        "transact_time": now.isoformat(),
    } if book else None
    return normalize_binary_event(
        raw_event(), venue=Venue.POLYMARKET, event_id="event-1",
        event_family="OTHER_BINARY_EVENT",
        event_question="Will the scheduled binary event resolve YES?",
        resolution_reference="official://event-1", book=book_data,
        observed_at=now.isoformat(),
    )


@pytest.mark.parametrize("probability", [-0.1, 0.0, 1.0, 1.1, math.nan, math.inf, -math.inf])
def test_event_forecast_probability_is_strictly_finite_and_bounded(probability):
    with pytest.raises(ValueError):
        forecast(probability=probability).validate()


def test_event_forecast_requires_method_and_version():
    with pytest.raises(ValueError):
        forecast(method_name="").validate()
    with pytest.raises(ValueError):
        forecast(method_version="").validate()


def test_static_provider_is_exact_and_validates():
    provider = StaticEventForecastProvider({"event-1": forecast()})
    assert provider.supports("event-1", "OTHER_BINARY_EVENT")
    assert provider.forecast("event-1", "OTHER_BINARY_EVENT").probability == 0.63
    assert not provider.supports("event-1", "CORPORATE")
    with pytest.raises(ValueError):
        provider.forecast("other", "OTHER_BINARY_EVENT")


def test_generic_normalization_preserves_identity_and_does_not_fabricate_price():
    normalized = market(book=False)
    assert normalized.venue == Venue.POLYMARKET
    assert normalized.venue_market_id == "market-1"
    assert normalized.outcomes == {"YES": "YES", "NO": "NO"}
    assert normalized.yes_ask is None and normalized.no_ask is None
    assert normalized.executable_depth == {"YES": (), "NO": ()}
    assert normalized.original_metadata["event_identity"]["event_id"] == "event-1"


def test_generic_kalshi_normalization_and_malformed_rejection():
    raw = {
        "ticker": "KXEVENT-1", "title": "Will the scheduled binary event resolve YES?",
        "rules_primary": "Official binary result rules.", "status": "active",
        "yes_sub_title": "YES", "no_sub_title": "NO",
    }
    normalized = normalize_binary_event(
        raw, venue=Venue.KALSHI, event_id="event-1", event_family="OTHER_BINARY_EVENT",
        event_question="Will the scheduled binary event resolve YES?",
        resolution_reference="official://event-1",
        book={"orderbook_fp": {"yes_dollars": [["0.55", "100"]], "no_dollars": [["0.40", "100"]]}},
    )
    assert normalized.yes_ask == pytest.approx(0.6)
    with pytest.raises(ValueError):
        normalize_binary_event({}, venue=Venue.KALSHI, event_id="e", event_family="OTHER_BINARY_EVENT", event_question="q", resolution_reference="r")


def test_exact_identity_rejects_passage_enactment_and_ambiguous_contracts():
    exact = bind_forecast(forecast(), market())
    assert exact.status == "MATCHED"
    assert bind_forecast(forecast(event_id="final-passage"), market()).status == "NO_EXACT_CONTRACT"
    assert bind_forecast(forecast(authoritative_resolution_reference="official://enactment"), market()).status == "NO_EXACT_CONTRACT"
    assert bind_forecast(forecast(forecasted_outcome="MAYBE"), market()).status == "NO_EXACT_CONTRACT"
    assert bind_forecast(forecast(event_family="CORPORATE"), market()).status == "NO_EXACT_CONTRACT"
    assert bind_forecast(forecast(event_question="different question"), market()).status == "NO_EXACT_CONTRACT"


def test_qualification_reuses_existing_play_and_missing_price_cannot_buy():
    now = utcnow()
    exact_market = market(now)
    result = evaluate_event_forecast(exact_market, forecast(forecasted_at=(now - timedelta(seconds=1)).isoformat()), now=now)
    assert result.binding and result.binding.status == "MATCHED"
    assert result.play is not None
    missing = evaluate_event_forecast(market(now, book=False), forecast(forecasted_at=(now - timedelta(seconds=1)).isoformat()), now=now)
    assert missing.play is not None and missing.play.suggested_action.value != "BUY"
    no_contract = evaluate_event_forecast(exact_market, forecast(event_id="other"), now=now)
    assert no_contract.status == "NO_EXACT_CONTRACT" and no_contract.play is None


def test_stale_forecast_and_missing_liquidity_are_safe():
    now = utcnow()
    stale = evaluate_event_forecast(
        market(now), forecast(forecasted_at=(now - timedelta(minutes=5)).isoformat()), now=now
    )
    assert stale.play is not None and stale.play.suggested_action.value != "BUY"
    no_liquidity = normalize_binary_event(
        raw_event(), venue=Venue.POLYMARKET, event_id="event-1",
        event_family="OTHER_BINARY_EVENT",
        event_question="Will the scheduled binary event resolve YES?",
        resolution_reference="official://event-1", book={
            "market-1::YES": {"best_ask": 0.58, "best_bid": 0.55},
            "market-1::NO": {"best_ask": 0.43, "best_bid": 0.40},
        }, observed_at=now.isoformat(),
    )
    result = evaluate_event_forecast(no_liquidity, forecast(forecasted_at=(now - timedelta(seconds=1)).isoformat()), now=now)
    assert result.play is not None and result.play.suggested_action.value != "BUY"


def test_capture_freezes_event_forecast_immutably_without_publication(tmp_path):
    now = utcnow() - timedelta(seconds=1)
    store = TrackRecord(tmp_path / "record.sqlite")
    observation = capture_event_forecast(store, market(now), forecast(forecasted_at=now.isoformat()), now=now)
    assert observation["event_family"] == "OTHER_BINARY_EVENT"
    assert observation["event_forecast"]["method_version"] == "bounded-input-v1"
    assert observation["evidence_snapshot"]["forecast_metadata"]["probability"] == 0.63
    assert store.summary()["published_plays"] == 0
    with store.connect() as db:
        db.execute("SELECT 1")
        before = db.execute("SELECT snapshot FROM prospective_plays").fetchone()[0]
    assert capture_event_forecast(store, market(now), forecast(forecasted_at=now.isoformat()), now=now) == observation
    with store.connect() as db:
        assert db.execute("SELECT snapshot FROM prospective_plays").fetchone()[0] == before


def test_clarity_acceptance_fixture_preserves_external_forecast_without_contract(tmp_path):
    """The CLARITY values are caller-supplied acceptance inputs, never model logic."""
    now = utcnow()
    question = "U.S. Senate cloture on the motion to proceed to H.R. 3633"
    resolution = "Official U.S. Senate roll call for the September 15, 2026 cloture vote on the motion to proceed to H.R. 3633."
    reference = "official://senate-roll-call/2026-09-15/hr-3633-cloture"

    def clarity(outcome, probability):
        return EventForecast(
            event_id="clarity-act-cloture-hr3633-2026-09-15",
            event_family="LEGISLATIVE_REGULATORY",
            event_question=question,
            forecasted_outcome=outcome,
            probability=probability,
            method_name="coalition-state-v0",
            method_version="clarity-coalition-v0",
            confidence="MEDIUM_LOW",
            evidence_references=("acceptance-fixture://clarity",),
            assumptions=("60 votes required for cloture",),
            invalidation_conditions=("vote is postponed or resolution semantics change",),
            forecasted_at=(now - timedelta(seconds=1)).isoformat(),
            authoritative_resolution=resolution,
            authoritative_resolution_reference=reference,
            expected_outcome="FAIL",
        )

    pass_forecast = clarity("PASS", 0.4105)
    fail_forecast = clarity("FAIL", 0.5895)
    assert pass_forecast.as_metadata()["probability"] == 0.4105
    assert fail_forecast.as_metadata()["probability"] == 0.5895
    assert pass_forecast.probability + fail_forecast.probability == pytest.approx(1.0)
    assert fail_forecast.expected_outcome == "FAIL"

    contract = normalize_binary_event(
        {
            "id": "unverified-market",
            "question": question,
            "description": "Resolves according to the official Senate roll call.",
            "marketSides": [{"long": True, "description": "PASS"}, {"long": False, "description": "FAIL"}],
            "event_identity": {
                "event_id": "different-contract",
                "event_family": "LEGISLATIVE_REGULATORY",
                "event_question": question,
                "resolution_reference": reference,
            },
        },
        venue=Venue.POLYMARKET,
        event_id="different-contract",
        event_family="LEGISLATIVE_REGULATORY",
        event_question=question,
        resolution_reference=reference,
        observed_at=now.isoformat(),
    )
    evaluation = evaluate_event_forecast(contract, fail_forecast, now=now)
    assert evaluation.status == "NO_EXACT_CONTRACT"
    assert evaluation.play is None
    assert contract.yes_ask is None and contract.no_ask is None
    assert contract.executable_depth == {"YES": (), "NO": ()}
    with pytest.raises(ValueError, match="NO_EXACT_CONTRACT"):
        capture_event_forecast(TrackRecord(tmp_path / "clarity.sqlite"), contract, fail_forecast, now=now)
