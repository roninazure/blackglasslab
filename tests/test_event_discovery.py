from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from parallax.event_discovery import (
    DiscoveryStatus,
    EnrichmentStatus,
    MatchStatus,
    classify_event_family,
    discover_event_candidate,
    enrich_event_candidate,
    match_event_contract,
    normalized_for_exact_match,
)
from parallax.generic_events import EventFamily, EventForecast, evaluate_event_forecast
from parallax.models import Venue

NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)
QUESTION = "Will the Senate invoke cloture on the motion to proceed to H.R. 3633 on Sep 15, 2026?"
RULES = (
    "This market resolves YES if the U.S. Senate invokes cloture on the motion "
    "to proceed to H.R. 3633 on September 15, 2026. Resolution uses the official "
    "U.S. Senate roll call; 60 votes are required."
)


def pmus(
    question: str = QUESTION,
    rules: str = RULES,
    *,
    market_id: str = "clarity-cloture",
    category: str = "Politics",
    yes: str = "YES",
    no: str = "NO",
    **extra,
):
    return {
        "id": market_id,
        "slug": market_id,
        "question": question,
        "description": rules,
        "category": category,
        "active": True,
        "closed": False,
        "marketSides": [
            {"long": True, "description": yes},
            {"long": False, "description": no},
        ],
        **extra,
    }


def candidate(raw=None, **kwargs):
    decision = discover_event_candidate(
        raw or pmus(), venue=Venue.POLYMARKET, discovered_at=NOW, **kwargs
    )
    assert decision.status is DiscoveryStatus.CANDIDATE
    assert decision.candidate is not None
    return decision.candidate


def test_discovery_accepts_binary_and_excludes_sports_and_malformed():
    assert candidate().event_family is EventFamily.LEGISLATIVE_REGULATORY
    sports = discover_event_candidate(
        pmus("Will the Yankees win?", "Resolves from the official MLB result.", category="MLB"),
        venue=Venue.POLYMARKET,
    )
    assert sports.status is DiscoveryStatus.UNSUPPORTED
    assert sports.reasons == ("sports_market_excluded",)
    opaque_sport = discover_event_candidate(
        pmus("Named award winner", "Resolves from the official result.", category="sports"),
        venue=Venue.POLYMARKET,
    )
    assert opaque_sport.reasons == ("sports_market_excluded",)
    malformed = discover_event_candidate({}, venue=Venue.POLYMARKET)
    assert malformed.status is DiscoveryStatus.UNSUPPORTED
    assert malformed.reasons == ("missing_market_id_question_or_rules",)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ({**pmus(), "marketSides": [{"long": True, "description": "YES"}]}, "not_explicitly_binary"),
        (pmus("How many Senators vote for H.R. 3633?", RULES), "count_contract"),
        (pmus(extra="ignored", yes_price="not-a-price"), None),
    ],
)
def test_malformed_or_count_contracts_are_rejected_without_inventing_values(raw, reason):
    decision = discover_event_candidate(raw, venue=Venue.POLYMARKET)
    if reason:
        assert decision.status is DiscoveryStatus.UNSUPPORTED
        assert reason in decision.reasons
    else:
        assert decision.candidate is not None
        assert decision.candidate.executable_prices["YES"] is None


def test_closed_and_malformed_price_are_unsupported():
    closed = discover_event_candidate(pmus(active=False, closed=True), venue=Venue.POLYMARKET)
    assert closed.reasons == ("stale_or_closed_market",)
    malformed = discover_event_candidate(pmus(yes_price=1.4), venue=Venue.POLYMARKET)
    assert malformed.reasons == ("malformed_price",)


@pytest.mark.parametrize(
    ("text", "family"),
    [
        ("Will Congress pass the bill?", EventFamily.LEGISLATIVE_REGULATORY),
        ("Will the Federal Reserve cut its interest rate?", EventFamily.MACRO_MONETARY),
        ("Will a candidate win the election?", EventFamily.ELECTION_POLITICAL),
        ("Will the Supreme Court issue a ruling?", EventFamily.LEGAL_JUDICIAL),
        ("Will the company complete an acquisition?", EventFamily.CORPORATE),
        ("Will the countries agree to a ceasefire?", EventFamily.GEOPOLITICAL),
        ("Will the named ceremony occur?", EventFamily.OTHER_BINARY_EVENT),
    ],
)
def test_event_family_classification_is_conservative(text, family):
    raw = pmus(text, f"Official rules: {text}", category="events")
    assert classify_event_family(raw) is family


def test_clarity_exact_procedural_contract():
    target = candidate()
    exact = candidate(pmus(market_id="exact-contract"))
    result = match_event_contract(target, exact)
    assert result.status is MatchStatus.EXACT
    assert {"action", "subject", "body", "stage", "deadline"} <= set(result.compared_dimensions)


@pytest.mark.parametrize(
    ("question", "rules", "expected_reason"),
    [
        (
            "Will the Senate pass H.R. 3633 on Sep 15, 2026?",
            "Resolves YES if the U.S. Senate passes H.R. 3633 on September 15, 2026 according to the official U.S. Senate roll call.",
            "action_mismatch",
        ),
        (
            "Will H.R. 3633 be signed into law in 2026?",
            "Resolves YES if H.R. 3633 is signed into law on September 15, 2026 according to the official U.S. Senate roll call.",
            "action_mismatch",
        ),
        (
            "Will the Senate vote on H.R. 3633 before Oct 1, 2026?",
            "Resolves YES if the U.S. Senate holds a vote on H.R. 3633 before October 1, 2026 according to the official U.S. Senate roll call.",
            "action_mismatch",
        ),
        (
            "Will Senator Smith vote for H.R. 3633 on Sep 15, 2026?",
            "Resolves YES if Senator Smith votes for H.R. 3633 on September 15, 2026 according to the official U.S. Senate roll call.",
            "action_mismatch",
        ),
    ],
)
def test_clarity_near_matches_are_mismatches(question, rules, expected_reason):
    result = match_event_contract(candidate(), candidate(pmus(question, rules, market_id=question[:20])))
    assert result.status is MatchStatus.MISMATCH
    assert any(reason.startswith(expected_reason) for reason in result.reasons)


def test_semantic_dimension_mismatches_are_explicit():
    target = candidate()
    subject = candidate(pmus(QUESTION.replace("3633", "9999"), RULES.replace("3633", "9999")))
    deadline = candidate(pmus(QUESTION.replace("Sep 15", "Sep 16"), RULES.replace("September 15", "September 16")))
    actor = replace(target, identity=replace(target.identity, actor="house", body="house"))
    outcome = replace(target, identity=replace(target.identity, outcome="fails"))
    assert any(x.startswith("subject_mismatch") for x in match_event_contract(target, subject).reasons)
    assert any(x.startswith("deadline_mismatch") for x in match_event_contract(target, deadline).reasons)
    assert any(x.startswith("actor_mismatch") for x in match_event_contract(target, actor).reasons)
    assert any(x.startswith("outcome_mismatch") for x in match_event_contract(target, outcome).reasons)


def test_overlapping_deadline_is_not_exact_when_window_operator_differs():
    before = candidate(
        pmus(
            QUESTION.replace("on Sep 15", "before Sep 15"),
            RULES.replace("on September 15", "before September 15"),
            market_id="before-same-date",
        )
    )
    result = match_event_contract(candidate(), before)
    assert result.status is MatchStatus.MISMATCH
    assert any(reason.startswith("temporal_scope_mismatch") for reason in result.reasons)


def test_insufficient_or_conflicting_rules_are_ambiguous():
    insufficient = candidate(pmus(rules="This market resolves according to official sources."))
    result = match_event_contract(candidate(), insufficient)
    assert result.status is MatchStatus.AMBIGUOUS
    assert "incomplete_resolution_semantics" in result.reasons

    conflicting = candidate(
        pmus(
            QUESTION.replace("invoke cloture on the motion to proceed to", "pass"),
            RULES,
            market_id="conflicting",
        )
    )
    result = match_event_contract(candidate(), conflicting)
    assert result.status is MatchStatus.AMBIGUOUS
    assert "conflicting_action" in result.reasons


def test_reversed_semantics_and_substring_collision_do_not_match():
    reversed_candidate = candidate(pmus(yes="NO - event fails", no="YES - event succeeds"))
    assert match_event_contract(candidate(), reversed_candidate).status is MatchStatus.AMBIGUOUS
    collision = candidate(
        pmus(
            "Will the Senate pass H.R. 36330 on Sep 15, 2026?",
            RULES.replace("H.R. 3633", "H.R. 36330").replace("invokes cloture on the motion to proceed to", "passes"),
            market_id="substring-collision",
        )
    )
    result = match_event_contract(candidate(), collision)
    assert result.status is MatchStatus.MISMATCH
    assert any(reason.startswith("subject_mismatch") for reason in result.reasons)


def test_missing_price_and_liquidity_remain_missing():
    discovered = candidate()
    assert discovered.executable_prices == {"YES": None, "NO": None}
    assert discovered.liquidity is None


def test_kalshi_book_prices_are_used_only_when_present():
    raw = {
        "ticker": "KXCLARITY-26SEP15",
        "title": QUESTION,
        "rules_primary": RULES,
        "status": "open",
        "yes_sub_title": "YES",
        "no_sub_title": "NO",
    }
    missing = discover_event_candidate(raw, venue=Venue.KALSHI, discovered_at=NOW).candidate
    assert missing and missing.executable_prices == {"YES": None, "NO": None}
    with_book = discover_event_candidate(
        raw,
        venue=Venue.KALSHI,
        book={"orderbook_fp": {"yes_dollars": [["0.55", "10"]], "no_dollars": [["0.40", "8"]]}},
        discovered_at=NOW,
    ).candidate
    assert with_book and with_book.executable_prices == pytest.approx({"YES": 0.60, "NO": 0.45})


def test_only_exact_candidate_can_feed_existing_generic_binding_path():
    target = candidate()
    exact = candidate(pmus(market_id="exact-binding"))
    exact_match = match_event_contract(target, exact)
    market = normalized_for_exact_match(exact, exact_match)
    forecast = EventForecast(
        event_id=exact.event_id,
        event_family=exact.event_family.value,
        event_question=exact.contract_question,
        forecasted_outcome="YES",
        probability=0.63,
        method_name="fixture-only",
        method_version="v1",
        confidence="TEST",
        evidence_references=("fixture://source",),
        assumptions=(),
        invalidation_conditions=(),
        forecasted_at=(NOW - timedelta(seconds=1)).isoformat(),
        authoritative_resolution=exact.resolution_text,
        authoritative_resolution_reference=exact.source_reference or "inventory://exact-binding",
    )
    evaluation = evaluate_event_forecast(market, forecast, now=NOW)
    assert evaluation.binding and evaluation.binding.status == "MATCHED"
    assert evaluation.play is not None
    assert evaluation.play.suggested_action.value != "BUY"

    mismatch = candidate(
        pmus(
            "Will the Senate pass H.R. 3633 on Sep 15, 2026?",
            RULES.replace("invokes cloture on the motion to proceed to", "passes"),
            market_id="wrong-stage",
        )
    )
    mismatch_result = match_event_contract(target, mismatch)
    with pytest.raises(ValueError, match="non-exact"):
        normalized_for_exact_match(mismatch, mismatch_result)


def clarity_inventory_candidate():
    return candidate(
        pmus(
            "Senate vote on CLARITY",
            "This market resolves according to the venue's published rules.",
            market_id="clarity-vague",
        )
    )


def clarity_detail(**changes):
    return {
        "market": {
            "id": "clarity-vague",
            "slug": "clarity-vague",
            "title": "Senate vote on CLARITY",
            "description": RULES,
            "active": True,
            "closed": False,
            "marketSides": [
                {"long": True, "description": "YES"},
                {"long": False, "description": "NO"},
            ],
            "updatedAt": "2026-09-14T12:01:00Z",
            **changes,
        }
    }


def test_authoritative_detail_enriches_without_erasing_provenance_or_fabricating():
    inventory = clarity_inventory_candidate()
    assert match_event_contract(inventory, inventory).status is MatchStatus.AMBIGUOUS
    result = enrich_event_candidate(
        inventory,
        clarity_detail(),
        fetched_at=NOW,
        source_reference="https://venue.example/market/clarity-vague",
    )
    assert result.status is EnrichmentStatus.ENRICHED
    enriched = result.candidate
    assert enriched.raw_provenance == inventory.raw_provenance
    assert enriched.enrichment_provenance["detail_response"] == clarity_detail()
    assert enriched.enrichment_provenance["inventory_ambiguity_flags"] == inventory.ambiguity_flags
    assert enriched.identity.subject == "hr:3633"
    assert enriched.identity.stage == "motion_to_proceed_cloture"
    assert enriched.identity.deadline == "2026-09-15"
    assert enriched.identity.resolution_authority == "us_senate_roll_call"
    assert enriched.open_time is None
    assert enriched.close_time is None
    assert enriched.resolution_time is None
    assert enriched.executable_prices == {"YES": None, "NO": None}
    assert enriched.liquidity is None
    assert match_event_contract(enriched, candidate()).status is MatchStatus.EXACT


@pytest.mark.parametrize(
    ("question", "rules"),
    [
        (
            "Will the Senate pass H.R. 3633 on Sep 15, 2026?",
            "Resolves YES if the Senate passes H.R. 3633 on September 15, 2026 according to the official U.S. Senate roll call.",
        ),
        (
            "Will H.R. 3633 be signed into law on Sep 15, 2026?",
            "Resolves YES if H.R. 3633 is signed into law on September 15, 2026 according to the official U.S. Senate roll call.",
        ),
        (
            "Will the Senate vote on H.R. 3633 on Sep 15, 2026?",
            "Resolves YES if the Senate holds a vote on H.R. 3633 on September 15, 2026 according to the official U.S. Senate roll call.",
        ),
        (
            "Will Senator Smith vote for H.R. 3633 on Sep 15, 2026?",
            "Resolves YES if Senator Smith votes for H.R. 3633 on September 15, 2026 according to the official U.S. Senate roll call.",
        ),
    ],
)
def test_clarity_enrichment_remains_mismatch_for_different_contracts(question, rules):
    enriched = enrich_event_candidate(clarity_inventory_candidate(), clarity_detail()).candidate
    assert match_event_contract(enriched, candidate(pmus(question, rules))).status is MatchStatus.MISMATCH


def test_clarity_enrichment_mismatches_vote_count_contract():
    enriched = enrich_event_candidate(clarity_inventory_candidate(), clarity_detail()).candidate
    count_contract = replace(candidate(), derivative_flags=("count_contract",))
    result = match_event_contract(enriched, count_contract)
    assert result.status is MatchStatus.MISMATCH
    assert result.reasons == ("contract_type_mismatch:binary!=derivative",)


def test_missing_or_partial_detail_leaves_candidate_present_and_ambiguous():
    inventory = clarity_inventory_candidate()
    malformed = enrich_event_candidate(inventory, {"market": []})
    assert malformed.status is EnrichmentStatus.UNCHANGED
    assert malformed.candidate is inventory
    partial = enrich_event_candidate(
        inventory,
        {"market": {"id": "clarity-vague", "title": "Senate vote on CLARITY"}},
    )
    assert partial.status is EnrichmentStatus.UNCHANGED
    assert match_event_contract(partial.candidate, partial.candidate).status is MatchStatus.AMBIGUOUS


def test_detail_can_remain_ambiguous_or_prove_mismatch():
    inventory = clarity_inventory_candidate()
    vague = clarity_detail(description="The official result determines resolution.")
    still = enrich_event_candidate(inventory, vague).candidate
    assert match_event_contract(still, still).status is MatchStatus.AMBIGUOUS
    target = candidate()
    passage = clarity_detail(
        title="Will the Senate pass H.R. 3633 on Sep 15, 2026?",
        description="Resolves YES if the Senate passes H.R. 3633 on September 15, 2026 according to the official U.S. Senate roll call.",
    )
    proved = enrich_event_candidate(inventory, passage).candidate
    assert match_event_contract(target, proved).status is MatchStatus.MISMATCH


@pytest.mark.parametrize(
    ("changes", "flag"),
    [
        (
            {
                "title": "Will the Senate pass H.R. 3633 on Sep 15, 2026?",
                "description": RULES,
            },
            "conflicting_action",
        ),
        ({"close_time": "2026-09-16T00:00:00Z"}, "inventory_detail_close_time_conflict"),
        ({"active": False, "status": "halted"}, "inventory_detail_status_conflict"),
        (
            {
                "marketSides": [
                    {"long": True, "description": "NO - event fails"},
                    {"long": False, "description": "YES - event succeeds"},
                ]
            },
            "reversed_binary_semantics",
        ),
        ({"eventSlug": "different-parent"}, "inventory_detail_event_conflict"),
    ],
)
def test_detail_conflicts_are_explicit_and_never_exact(changes, flag):
    inventory = clarity_inventory_candidate()
    inventory = replace(
        inventory,
        close_time="2026-09-15T23:59:00Z",
        raw_provenance={
            **inventory.raw_provenance,
            "market": {**inventory.raw_provenance["market"], "eventSlug": "inventory-parent"},
        },
    )
    result = enrich_event_candidate(inventory, clarity_detail(**changes))
    assert result.status is EnrichmentStatus.ENRICHED
    assert flag in (*result.candidate.ambiguity_flags, *result.candidate.enrichment_conflict_flags)
    assert match_event_contract(result.candidate, result.candidate).status is MatchStatus.AMBIGUOUS


def test_parent_child_and_resolution_authority_conflicts_are_auditable():
    inventory = clarity_inventory_candidate()
    parent = {
        "title": "Will the Senate finally pass H.R. 3633 on Sep 15, 2026?",
        "description": "Resolution uses the official U.S. Senate roll call.",
    }
    result = enrich_event_candidate(inventory, clarity_detail(), event=parent)
    assert "parent_child_action_conflict" in result.candidate.enrichment_conflict_flags
    authority_inventory = replace(
        inventory,
        identity=replace(inventory.identity, resolution_authority="official_court_record"),
    )
    authority = enrich_event_candidate(authority_inventory, clarity_detail()).candidate
    assert "inventory_detail_resolution_authority_conflict" in authority.enrichment_conflict_flags


def test_different_vague_title_with_same_rules_can_be_exact():
    detail = clarity_detail(title="Procedural contract for the chamber")
    enriched = enrich_event_candidate(clarity_inventory_candidate(), detail).candidate
    assert match_event_contract(enriched, candidate()).status is MatchStatus.EXACT


def test_series_collision_and_close_time_are_audited_without_becoming_deadline():
    inventory = clarity_inventory_candidate()
    inventory = replace(
        inventory,
        raw_provenance={
            **inventory.raw_provenance,
            "market": {**inventory.raw_provenance["market"], "series_ticker": "SERIES-A"},
        },
    )
    result = enrich_event_candidate(
        inventory,
        clarity_detail(series_ticker="SERIES-B", close_time="2026-09-16T00:00:00Z"),
    )
    assert "inventory_detail_series_conflict" in result.candidate.enrichment_conflict_flags
    assert result.candidate.identity.deadline == "2026-09-15"
    assert result.candidate.close_time == "2026-09-16T00:00:00Z"


def test_inventory_detail_deadline_conflict_is_explicit():
    inventory = candidate(
        pmus(
            "Will the Senate invoke cloture on the motion to proceed to H.R. 3633 on Sep 14, 2026?",
            RULES.replace("September 15", "September 14"),
            market_id="clarity-vague",
        )
    )
    result = enrich_event_candidate(inventory, clarity_detail())
    assert "inventory_detail_deadline_conflict" in result.candidate.enrichment_conflict_flags
    assert match_event_contract(result.candidate, result.candidate).status is MatchStatus.AMBIGUOUS


def test_missing_resolution_authority_remains_ambiguous_and_derivatives_unsupported():
    detail = clarity_detail(description=RULES.replace("official U.S. Senate roll call", "published results"))
    enriched = enrich_event_candidate(clarity_inventory_candidate(), detail).candidate
    assert enriched.identity.resolution_authority is None
    assert match_event_contract(enriched, enriched).status is MatchStatus.AMBIGUOUS
    derivative = replace(candidate(), derivative_flags=("count_contract",))
    assert match_event_contract(derivative, derivative).status is MatchStatus.UNSUPPORTED


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"updatedAt": "not-a-timestamp"}, "malformed_detail_timestamp:updatedAt"),
        ({"strike_value": "sixty"}, "malformed_detail_threshold:strike_value"),
        ({"yes_price": 1.2}, "malformed_detail_price"),
        ({"liquidity": "unknown"}, "malformed_detail_liquidity"),
    ],
)
def test_malformed_detail_is_rejected_without_overwriting_inventory(changes, reason):
    inventory = clarity_inventory_candidate()
    result = enrich_event_candidate(inventory, clarity_detail(**changes))
    assert result.status is EnrichmentStatus.UNCHANGED
    assert result.reasons == (reason,)
    assert result.candidate is inventory


def test_closed_detail_is_unsupported_but_keeps_both_provenances():
    inventory = clarity_inventory_candidate()
    result = enrich_event_candidate(inventory, clarity_detail(active=False, closed=True))
    assert result.status is EnrichmentStatus.UNSUPPORTED
    assert result.candidate.raw_provenance == inventory.raw_provenance
    assert result.candidate.enrichment_provenance["detail_market"]["closed"] is True


def test_markup_is_removed_and_detail_economics_only_populate_when_present():
    detail = clarity_detail(
        description=f"<p>{RULES}</p>",
        yes_price="0.61",
        liquidity="120.5",
    )
    enriched = enrich_event_candidate(clarity_inventory_candidate(), detail).candidate
    assert "<p>" not in enriched.resolution_text
    assert enriched.executable_prices == {"YES": 0.61, "NO": None}
    assert enriched.liquidity == 120.5


def test_enriched_exact_normalization_uses_detail_rules_and_guards_other_states():
    target = candidate()
    exact = enrich_event_candidate(clarity_inventory_candidate(), clarity_detail()).candidate
    market = normalized_for_exact_match(exact, match_event_contract(target, exact))
    assert market.resolution_rules == exact.resolution_text
    ambiguous = clarity_inventory_candidate()
    with pytest.raises(ValueError, match="non-exact"):
        normalized_for_exact_match(ambiguous, match_event_contract(target, ambiguous))
    mismatch = enrich_event_candidate(
        clarity_inventory_candidate(),
        clarity_detail(
            title="Will the Senate pass H.R. 3633 on Sep 15, 2026?",
            description="Resolves YES if the Senate passes H.R. 3633 on September 15, 2026 according to the official U.S. Senate roll call.",
        ),
    ).candidate
    with pytest.raises(ValueError, match="non-exact"):
        normalized_for_exact_match(mismatch, match_event_contract(target, mismatch))
    unsupported = replace(exact, derivative_flags=("count_contract",))
    with pytest.raises(ValueError, match="non-exact"):
        normalized_for_exact_match(unsupported, match_event_contract(unsupported, unsupported))


def test_sports_remain_excluded_before_detail_lookup():
    sports = discover_event_candidate(
        pmus("Will the Yankees win?", "Official MLB result.", category="MLB"),
        venue=Venue.POLYMARKET,
    )
    assert sports.status is DiscoveryStatus.UNSUPPORTED
    assert sports.candidate is None
