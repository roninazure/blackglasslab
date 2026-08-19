from live_runner import _merge_inference_slugs, _pipeline_market_record


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
