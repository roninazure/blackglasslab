from loop_engine.config import LoopEngineConfig
from llm.routing import route_model
from pathlib import Path
from live_runner import (
    _llm_allocation_priority,
    _merge_inference_slugs,
    _pipeline_market_record,
)


def test_ranked_dynamic_discovery_enters_bounded_batch():
    fixed = ["fixed-a", "fixed-b", "fixed-c"]
    dynamic = ["best-1", "best-2", "best-3", "best-4"]

    selected = _merge_inference_slugs(fixed, dynamic, 3)

    assert selected == ["best-1", "best-2", "fixed-a"]


def test_selection_never_exceeds_batch():
    selected = _merge_inference_slugs(
        ["fixed-a", "fixed-b"],
        ["d1", "d2", "d3", "d4", "d5"],
        4,
    )

    assert len(selected) == 4
    assert selected == ["d1", "d2", "d3", "fixed-a"]


def test_empty_discovery_preserves_fixed_fallback():
    assert _merge_inference_slugs(
        ["fixed-a", "fixed-b", "fixed-c"],
        [],
        2,
    ) == ["fixed-a", "fixed-b"]


def test_dynamic_only_selection_is_rank_preserving():
    assert _merge_inference_slugs(
        [],
        ["best", "second", "third"],
        2,
    ) == ["best", "second"]


def test_duplicate_dynamic_and_fixed_market_is_not_repeated():
    selected = _merge_inference_slugs(
        ["same", "fixed-b"],
        ["same", "dynamic-b"],
        3,
    )

    assert selected == ["same", "dynamic-b", "fixed-b"]
    assert len(selected) == len(set(selected))


def test_dynamic_pipeline_record_is_complete():
    record = _pipeline_market_record("dynamic-market")

    assert record["market_id"] == "dynamic-market"
    assert record["reason"] == "unclassified"
    assert record["brain"]["market_id"] == "dynamic-market"
    assert record["brain"]["policy_allowed"] is None
    assert record["brain"]["llm_used"] is False


def _priority(**overrides):
    values = {
        "opportunity_score": 80.0,
        "baseline_probability": 0.52,
        "market_probability": 0.50,
        "liquidity": 100000.0,
        "spread": 0.01,
        "time_to_resolution_days": 60.0,
    }
    values.update(overrides)
    return _llm_allocation_priority(**values)["priority"]


def test_llm_allocation_rewards_meaningful_baseline_disagreement():
    higher_quality_tiny_edge = _priority(
        opportunity_score=82.0,
        baseline_probability=0.505,
    )
    slightly_lower_quality_better_edge = _priority(
        opportunity_score=79.0,
        baseline_probability=0.52,
    )

    assert slightly_lower_quality_better_edge > higher_quality_tiny_edge


def test_llm_allocation_rewards_liquidity():
    assert _priority(liquidity=300000.0) > _priority(liquidity=10000.0)


def test_llm_allocation_penalizes_wide_spread():
    assert _priority(spread=0.002) > _priority(spread=0.03)


def test_llm_allocation_rewards_shorter_resolution():
    assert _priority(
        time_to_resolution_days=7.0
    ) > _priority(
        time_to_resolution_days=250.0
    )


def test_llm_allocation_near_zero_edge_does_not_zero_priority():
    result = _llm_allocation_priority(
        opportunity_score=80.0,
        baseline_probability=0.50001,
        market_probability=0.50,
        liquidity=100000.0,
        spread=0.01,
        time_to_resolution_days=30.0,
    )

    assert result["priority"] > 0.0
    assert result["edge_signal"] >= 0.25


def test_llm_allocation_is_deterministic_and_bounded():
    first = _llm_allocation_priority(
        opportunity_score=81.0,
        baseline_probability=0.54,
        market_probability=0.50,
        liquidity=250000.0,
        spread=0.005,
        time_to_resolution_days=14.0,
    )
    second = _llm_allocation_priority(
        opportunity_score=81.0,
        baseline_probability=0.54,
        market_probability=0.50,
        liquidity=250000.0,
        spread=0.005,
        time_to_resolution_days=14.0,
    )

    assert first == second
    assert 0.0 <= first["priority"] <= 1.0


def test_skeptic_failure_semantics_are_not_reject_semantics():
    source = Path("live_runner.py").read_text()

    assert '"action": "UNAVAILABLE"' in source
    assert 'rejection_reason="skeptic_unavailable"' in source
    assert 'production_decision="not_evaluated_for_production"' in source
    assert 'count_rejection("skeptic_unavailable")' in source

    # Genuine critic rejection must still remain distinct.
    assert 'if review.action == "REJECT":' in source
    assert 'rejection_reason="skeptic_reject"' in source


def test_shadow_audit_evidence_is_persisted():
    source = Path("live_runner.py").read_text()

    assert '"llm_rationale": item.get("llm_rationale")' in source
    assert '"llm_confidence": item.get("llm_confidence")' in source
    assert '"llm_model": item.get("llm_model")' in source
    assert '"llm_routing_tier": item.get("llm_routing_tier")' in source
    assert '"temporal_validation_reason": item.get(' in source
    assert '"temporal_validation_details": item.get(' in source

    assert 'item["llm_rationale"] = llm_rationale' in source
    assert 'item["temporal_validation_reason"] = temporal_reason' in source
    assert 'item["temporal_validation_details"] = temporal_details' in source


def test_skeptic_routing_uses_cheap_model_below_high_edge_threshold():
    config = LoopEngineConfig(
        skeptic_model="claude-haiku-test",
        finalist_model="claude-sonnet-test",
        skeptic_finalist_edge_threshold=0.08,
    )

    route = route_model(
        opportunity_score=80.0,
        skeptic=True,
        edge_abs=0.04,
        config=config,
    )

    assert route.model == "claude-haiku-test"
    assert route.tier == "skeptic"


def test_skeptic_routing_escalates_high_edge_to_finalist():
    config = LoopEngineConfig(
        skeptic_model="claude-haiku-test",
        finalist_model="claude-sonnet-test",
        skeptic_cost_usd=0.002,
        finalist_cost_usd=0.008,
        skeptic_finalist_edge_threshold=0.08,
    )

    route = route_model(
        opportunity_score=79.0,
        skeptic=True,
        edge_abs=0.225,
        config=config,
    )

    assert route.model == "claude-sonnet-test"
    assert route.tier == "skeptic_finalist"
    assert route.estimated_cost_usd == 0.008


def test_skeptic_high_edge_threshold_is_boundary_inclusive():
    config = LoopEngineConfig(
        skeptic_model="claude-haiku-test",
        finalist_model="claude-sonnet-test",
        skeptic_finalist_edge_threshold=0.08,
    )

    route = route_model(
        opportunity_score=60.0,
        skeptic=True,
        edge_abs=0.08,
        config=config,
    )

    assert route.model == "claude-sonnet-test"
    assert route.tier == "skeptic_finalist"
