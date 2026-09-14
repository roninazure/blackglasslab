from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from parallax.direct_contracts import (
    DirectBindingStatus,
    DirectContractForecast,
    DirectForecastFreshnessPolicy,
    bind_direct_contract_forecast,
    capture_direct_contract_forecast,
    direct_contract_fingerprint,
    evaluate_direct_contract_forecast,
    normalized_for_direct_contract,
)
from parallax.event_discovery import discover_event_candidate
from parallax.generic_events import normalize_binary_event
from parallax.models import NormalizedMarket, Venue
from parallax.normalization import rules_digest
from parallax.track_record import TrackRecord

NOW = datetime(2026, 9, 14, 18, 0, tzinfo=UTC)
QUESTION = "Will the Senate invoke cloture on the motion to proceed to H.R. 3633 on Sep 15, 2026?"
RULES = (
    "Resolves PASS if the U.S. Senate invokes cloture on the motion to proceed to "
    "H.R. 3633 on September 15, 2026, according to the official Senate roll call."
)


def raw_contract(**changes):
    return {
        "id": "clarity-cloture-2026-09-15",
        "slug": "clarity-cloture-2026-09-15",
        "question": QUESTION,
        "description": RULES,
        "status": "active",
        "resolution_time": "2026-09-15T19:00:00+00:00",
        "marketSides": [
            {"long": True, "description": "PASS"},
            {"long": False, "description": "FAIL"},
        ],
        **changes,
    }


def contract(*, book: bool = True, raw=None) -> NormalizedMarket:
    row = raw or raw_contract()
    slug = row.get("slug", row.get("id"))
    book_data = (
        {
            f"{slug}::YES": {
                "best_bid": 0.30,
                "best_ask": 0.32,
                "bid_size_shares": 100,
                "ask_size_shares": 100,
            },
            f"{slug}::NO": {
                "best_bid": 0.67,
                "best_ask": 0.69,
                "bid_size_shares": 100,
                "ask_size_shares": 100,
            },
            "transact_time": NOW.isoformat(),
        }
        if book
        else {}
    )
    return normalize_binary_event(
        row,
        venue=Venue.POLYMARKET,
        event_id="semantic-reconstruction-not-required",
        event_family="LEGISLATIVE_REGULATORY",
        event_question=QUESTION,
        resolution_reference="https://venue.example/contracts/clarity-cloture-2026-09-15",
        book=book_data,
        observed_at=NOW.isoformat(),
    )


def forecast(market=None, **changes):
    market = market or contract()
    value = DirectContractForecast(
        venue=market.venue,
        contract_id=market.venue_market_id,
        canonical_question=market.title,
        outcome="PASS",
        probability=0.4105,
        expected_outcome="FAIL",
        confidence="MEDIUM_LOW",
        forecasted_at=(NOW - timedelta(seconds=1)).isoformat(),
        method="coalition-state-v0",
        method_version="clarity-coalition-v0",
        contract_fingerprint=direct_contract_fingerprint(market),
        rules_digest=rules_digest(market),
        resolution_reference="https://venue.example/contracts/clarity-cloture-2026-09-15/rules",
        evidence_references=("fixture://methodology", "fixture://official-schedule"),
        assumptions=("externally supplied acceptance input",),
        limitations=("supports only the named contract",),
        external_methodology_metadata={"input_status": "FROZEN"},
    )
    return replace(value, **changes)


@pytest.mark.parametrize("probability", [0.0, 0.4105, 1.0])
def test_direct_forecast_accepts_finite_probability_in_closed_unit_interval(probability):
    assert forecast(probability=probability).validate().probability == probability


@pytest.mark.parametrize("probability", [-0.1, 1.1, math.nan, math.inf, -math.inf])
def test_direct_forecast_rejects_invalid_probability(probability):
    with pytest.raises(ValueError):
        forecast(probability=probability).validate()


def test_direct_forecast_requires_method_version_contract_and_outcome():
    for changes in (
        {"method": ""},
        {"method_version": ""},
        {"contract_id": ""},
        {"outcome": ""},
    ):
        with pytest.raises(ValueError):
            forecast(**changes).validate()


def test_exact_direct_binding_produces_evidence_for_existing_qualifier():
    market = contract()
    binding = bind_direct_contract_forecast(forecast(market), market, now=NOW)
    assert binding.status is DirectBindingStatus.EXACT_CONTRACT
    assert binding.side.value == "YES"
    assert binding.evidence is not None
    assert binding.evidence.fair_probability == 0.4105
    assert binding.evidence.rules_digest == rules_digest(market)

    evaluation = evaluate_direct_contract_forecast(market, forecast(market), now=NOW)
    assert evaluation.binding is binding or evaluation.binding == binding
    assert evaluation.play is not None
    assert evaluation.play.evidence == binding.evidence


@pytest.mark.parametrize(
    ("forecast_changes", "market_changes", "reason"),
    [
        ({"contract_id": "different-id"}, {}, "identifier"),
        ({"venue": Venue.KALSHI}, {}, "venue"),
        ({"outcome": "YES"}, {}, "outcome"),
        ({"rules_digest": "0" * 64}, {}, "rules"),
        ({"contract_fingerprint": "0" * 64}, {}, "fingerprint"),
    ],
)
def test_any_exact_identity_difference_is_a_mismatch(forecast_changes, market_changes, reason):
    market = replace(contract(), **market_changes)
    result = bind_direct_contract_forecast(forecast(market), market, now=NOW)
    changed = replace(result.forecast, **forecast_changes)
    result = bind_direct_contract_forecast(changed, market, now=NOW)
    assert result.status is DirectBindingStatus.MISMATCH
    assert reason in result.reason
    assert result.evidence is None


def test_changed_rules_digest_invalidates_binding():
    original = contract()
    changed = replace(original, resolution_rules=original.resolution_rules + " Amended after forecast.")
    result = bind_direct_contract_forecast(forecast(original), changed, now=NOW)
    assert result.status is DirectBindingStatus.MISMATCH
    assert result.reason == "resolution rules digest differs"


def test_stale_forecast_and_closed_contract_cannot_produce_play():
    stale = forecast(forecasted_at=(NOW - timedelta(seconds=61)).isoformat())
    stale_result = evaluate_direct_contract_forecast(contract(), stale, now=NOW)
    assert stale_result.status == DirectBindingStatus.STALE.value
    assert stale_result.play is None

    market = contract()
    closed = replace(market, status="CLOSED")
    closed_forecast = replace(
        forecast(market),
        contract_fingerprint=direct_contract_fingerprint(closed),
        rules_digest=rules_digest(closed),
    )
    closed_result = evaluate_direct_contract_forecast(closed, closed_forecast, now=NOW)
    assert closed_result.status == DirectBindingStatus.UNSUPPORTED.value
    assert closed_result.play is None


@pytest.mark.parametrize(
    ("question", "rules", "resolution_time"),
    [
        (
            "Will the Senate pass H.R. 3633 on Sep 15, 2026?",
            "Resolves YES if the Senate passes H.R. 3633.",
            "2026-09-15T19:00:00+00:00",
        ),
        (
            "Will H.R. 3633 be signed into law in 2026?",
            "Resolves YES if H.R. 3633 is signed into law.",
            "2026-12-31T23:59:00+00:00",
        ),
        (
            "Will the Senate hold a vote on H.R. 3633 on Sep 15, 2026?",
            "Resolves YES if any Senate vote on H.R. 3633 occurs.",
            "2026-09-15T19:00:00+00:00",
        ),
        (
            "Will Senator X vote yes on H.R. 3633?",
            "Resolves YES if Senator X casts a yes vote.",
            "2026-09-15T19:00:00+00:00",
        ),
        (
            "Will the total YES vote count on H.R. 3633 exceed 59?",
            "Resolves YES if the total YES count exceeds 59.",
            "2026-09-15T19:00:00+00:00",
        ),
        (
            QUESTION,
            RULES,
            "2026-09-16T19:00:00+00:00",
        ),
        (
            QUESTION,
            "Same title, but resolves using a different rule and authority.",
            "2026-09-15T19:00:00+00:00",
        ),
    ],
)
def test_clarity_forecast_rejects_all_near_match_attacks(question, rules, resolution_time):
    exact_market = contract()
    near = contract(
        raw=raw_contract(question=question, description=rules, resolution_time=resolution_time)
    )
    evaluation = evaluate_direct_contract_forecast(near, forecast(exact_market), now=NOW)
    assert evaluation.status == DirectBindingStatus.MISMATCH.value
    assert evaluation.play is None


def test_missing_price_and_insufficient_liquidity_keep_existing_gates():
    missing = contract(book=False)
    missing_result = evaluate_direct_contract_forecast(missing, forecast(missing), now=NOW)
    assert missing_result.play is not None
    assert missing_result.play.suggested_action.value != "BUY"
    assert "invalid_price" in missing_result.play.verdict.failed_gates

    shallow = replace(
        contract(),
        executable_depth={"YES": ((0.32, 1.0),), "NO": ((0.69, 1.0),)},
    )
    shallow_forecast = replace(
        forecast(shallow),
        contract_fingerprint=direct_contract_fingerprint(shallow),
        rules_digest=rules_digest(shallow),
    )
    shallow_result = evaluate_direct_contract_forecast(shallow, shallow_forecast, now=NOW)
    assert shallow_result.play is not None
    assert shallow_result.play.suggested_action.value != "BUY"
    assert "liquidity" in shallow_result.play.verdict.failed_gates


def test_exact_observation_is_immutable_and_contains_no_execution_or_publication_claim(tmp_path):
    market = contract()
    value = forecast(market)
    store = TrackRecord(tmp_path / "direct-contract.sqlite")
    observation = capture_direct_contract_forecast(store, market, value, now=NOW)
    frozen = observation["evidence_snapshot"]["forecast_metadata"]
    assert frozen["method"] == "coalition-state-v0"
    assert frozen["method_version"] == "clarity-coalition-v0"
    assert frozen["contract_fingerprint"] == direct_contract_fingerprint(market)
    assert frozen["probability"] == 0.4105
    assert frozen["execution_claim"] is False
    assert frozen["publication_claim"] is False
    assert store.summary()["published_plays"] == 0

    with store.connect() as db:
        before = db.execute("SELECT snapshot FROM prospective_plays").fetchone()[0]
        with pytest.raises(Exception, match="Immutable prospective play"):
            db.execute("UPDATE prospective_plays SET verdict='BUY'")
    assert capture_direct_contract_forecast(store, market, value, now=NOW) == observation
    with store.connect() as db:
        assert db.execute("SELECT snapshot FROM prospective_plays").fetchone()[0] == before


def test_candidate_can_normalize_without_abstract_semantic_exactness():
    decision = discover_event_candidate(raw_contract(), venue=Venue.POLYMARKET, discovered_at=NOW)
    assert decision.candidate is not None
    candidate = replace(
        decision.candidate,
        ambiguity_flags=("missing_exact_dimension:resolution_authority",),
    )
    market = normalized_for_direct_contract(candidate)
    assert market.venue_market_id == candidate.market_id
    assert market.title == candidate.contract_question
    assert market.resolution_rules == candidate.resolution_text


def test_explicit_freshness_policy_is_enforced():
    policy = DirectForecastFreshnessPolicy(max_age=timedelta(minutes=10))
    value = forecast(forecasted_at=(NOW - timedelta(minutes=5)).isoformat())
    assert bind_direct_contract_forecast(value, contract(), now=NOW, freshness=policy).status is DirectBindingStatus.EXACT_CONTRACT
