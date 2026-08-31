from __future__ import annotations

import ast
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from negrisk_economics import (
    BookLevel,
    FeeMetadata,
    FillEvidence,
    LegBook,
    ValidationConfig,
    compare_predictions,
    evaluate_basket,
    fee_metadata_from_venue,
    initialize_paper_db,
    record_outcome,
    record_prediction,
)
from scripts import negrisk_paper_smoke


ZERO_FEE = FeeMetadata(0.0, 0.0, True, "fixture_zero", True)
CURVE_FEE = FeeMetadata(0.02, 1.0, True, "fixture_fd", True)


def leg(token: str, prices: list[tuple[float, float]], *, at: str = "2026-08-16T12:00:02Z", fee: FeeMetadata = ZERO_FEE) -> LegBook:
    return LegBook(f"market-{token}", token, at, tuple(BookLevel(*row) for row in prices), fee)


def basket(prices: list[list[tuple[float, float]]], *, at: str, fee: FeeMetadata = ZERO_FEE) -> list[LegBook]:
    return [leg(str(index), levels, at=at, fee=fee) for index, levels in enumerate(prices)]


class NegRiskEconomicsTests(unittest.TestCase):
    def evaluate(self, initial_prices, execution_prices=None, **kwargs):
        execution_prices = execution_prices or initial_prices
        return evaluate_basket(
            event_id="event",
            initial_books=basket(initial_prices, at="2026-08-16T12:00:00Z", fee=kwargs.pop("fee", ZERO_FEE)),
            execution_books=basket(execution_prices, at="2026-08-16T12:00:02Z", fee=kwargs.pop("execution_fee", ZERO_FEE)),
            basket_complete=kwargs.pop("basket_complete", True),
            config=kwargs.pop("config", ValidationConfig(target_shares=5, min_shares=1, latency_seconds=1)),
            **kwargs,
        )

    def test_positive_gross_becomes_negative_after_observed_slippage_and_fees(self) -> None:
        initial = [[(0.33, 10)], [(0.33, 10)], [(0.33, 10)]]
        execution = [[(0.33, 1), (0.335, 9)]] * 3
        result = self.evaluate(
            initial,
            execution,
            execution_fee=FeeMetadata(0.02, 1.0, True, "fixture_fd", True),
            config=ValidationConfig(target_shares=10, min_shares=1, latency_seconds=1),
        )
        self.assertGreater(result.gross_edge_usd, 0)
        self.assertGreater(result.slippage_usd, 0)
        self.assertGreater(result.fee_usd, 0)
        self.assertLess(result.net_executable_edge_usd, 0)
        self.assertEqual(result.status, "REJECTED_NET_EDGE")

    def test_depth_limited_basket_sizing_uses_common_share_depth(self) -> None:
        prices = [[(0.30, 10)], [(0.30, 4)], [(0.30, 8)]]
        result = self.evaluate(
            prices,
            config=ValidationConfig(target_shares=10, min_shares=1, latency_seconds=1, allow_depth_limited_size=True),
        )
        self.assertEqual(result.simultaneous_depth_shares, 4)
        self.assertEqual(result.sized_shares, 4)
        self.assertEqual(result.paper_filled_shares, 4)
        self.assertAlmostEqual(result.simultaneous_depth_usd, 3.6)

    def test_partial_basket_depth_is_rejected_without_any_paper_fill(self) -> None:
        prices = [[(0.30, 10)], [(0.30, 4)], [(0.30, 8)]]
        result = self.evaluate(
            prices,
            config=ValidationConfig(target_shares=10, min_shares=1, latency_seconds=1, allow_depth_limited_size=False),
        )
        self.assertEqual(result.status, "REJECTED_PARTIAL_BASKET_RISK")
        self.assertEqual(result.paper_filled_shares, 0)

    def test_missing_leg_is_rejected(self) -> None:
        initial = basket([[(0.3, 10)]] * 3, at="2026-08-16T12:00:00Z")
        execution = basket([[(0.3, 10)]] * 2, at="2026-08-16T12:00:02Z")
        result = evaluate_basket(event_id="event", initial_books=initial, execution_books=execution, basket_complete=True)
        self.assertEqual(result.status, "REJECTED_INCOMPLETE_BASKET")

    def test_unproven_basket_completeness_remains_unknown(self) -> None:
        result = self.evaluate([[(0.30, 10)]] * 3, basket_complete=None)
        self.assertEqual(result.status, "UNKNOWN_BASKET_COMPLETENESS")
        self.assertIsNone(result.net_executable_edge_usd)

    def test_latency_invalidates_opportunity(self) -> None:
        initial = [[(0.30, 10)], [(0.30, 10)], [(0.30, 10)]]
        execution = [[(0.35, 10)], [(0.34, 10)], [(0.33, 10)]]
        result = self.evaluate(initial, execution)
        self.assertTrue(result.signal_gross_edge_per_share > 0)
        self.assertFalse(result.opportunity_survived_latency)
        self.assertEqual(result.status, "REJECTED_NET_EDGE")

    def test_unobserved_configured_latency_remains_unknown(self) -> None:
        prices = [[(0.30, 10)]] * 3
        result = evaluate_basket(
            event_id="event",
            initial_books=basket(prices, at="2026-08-16T12:00:00Z"),
            execution_books=basket(prices, at="2026-08-16T12:00:00.5Z"),
            basket_complete=True,
            config=ValidationConfig(target_shares=5, min_shares=1, latency_seconds=1),
        )
        self.assertEqual(result.status, "UNKNOWN_LATENCY_NOT_OBSERVED")
        self.assertIsNone(result.opportunity_survived_latency)

    def test_authoritative_fee_curve_and_calculation(self) -> None:
        metadata = fee_metadata_from_venue({}, clob_market_info={"fd": {"r": 0.02, "e": 1, "to": True}})
        self.assertTrue(metadata.calculable)
        prices = [[(0.20, 5)], [(0.30, 5)], [(0.40, 5)]]
        result = self.evaluate(prices, fee=metadata, execution_fee=metadata)
        self.assertAlmostEqual(result.fee_usd, 5 * 0.02 * (0.16 + 0.21 + 0.24))
        unknown = fee_metadata_from_venue({"feesEnabled": True}, token_fee={"base_fee": 30})
        self.assertFalse(unknown.calculable)

    def test_slippage_walks_observable_levels(self) -> None:
        prices = [[(0.30, 2), (0.35, 3)]] * 3
        result = self.evaluate(prices)
        self.assertAlmostEqual(result.slippage_usd, 0.45)
        self.assertAlmostEqual(result.walked_cost_usd, 4.95)

    def test_unknown_fill_evidence_remains_unknown(self) -> None:
        result = self.evaluate([[(0.30, 10)]] * 3)
        self.assertIsNone(result.fill_probability)
        self.assertIsNone(result.fill_adjusted_net_edge_usd)
        self.assertEqual(result.fill_probability_status, "UNKNOWN_NO_EMPIRICAL_FILL_EVIDENCE")
        sparse = self.evaluate([[(0.30, 10)]] * 3, fill_evidence=FillEvidence(8, 10, "paper_trials"))
        self.assertIsNone(sparse.fill_probability)
        self.assertEqual(sparse.fill_probability_status, "UNKNOWN_INSUFFICIENT_FILL_TRIALS")

    def test_sufficient_fill_evidence_uses_conservative_lower_bound(self) -> None:
        result = self.evaluate([[(0.30, 100)]] * 3, fill_evidence=FillEvidence(90, 100, "paper_trials_v1"))
        self.assertGreater(result.fill_probability, 0)
        self.assertLess(result.fill_probability, 0.9)
        self.assertEqual(result.fill_probability_status, "MEASURED_WILSON_LOWER_BOUND")

    def test_net_edge_is_deterministic(self) -> None:
        prices = [[(0.20, 2), (0.22, 5)], [(0.30, 3), (0.31, 5)], [(0.40, 4), (0.42, 5)]]
        one = self.evaluate(prices, fee=CURVE_FEE, execution_fee=CURVE_FEE)
        two = self.evaluate(prices, fee=CURVE_FEE, execution_fee=CURVE_FEE)
        self.assertEqual(one, two)
        self.assertAlmostEqual(one.net_executable_edge_usd, one.sized_shares - one.walked_cost_usd - one.fee_usd)

    def test_unknown_fee_preserves_unknown_net_edge(self) -> None:
        unknown = FeeMetadata(None, None, None, "UNKNOWN", False)
        result = self.evaluate([[(0.30, 10)]] * 3, fee=unknown, execution_fee=unknown)
        self.assertEqual(result.status, "UNKNOWN_FEE_ECONOMICS")
        self.assertIsNone(result.fee_usd)
        self.assertIsNone(result.net_executable_edge_usd)

    def test_paper_record_supports_later_prediction_comparison(self) -> None:
        result = self.evaluate([[(0.30, 10)]] * 3)
        with sqlite3.connect(":memory:") as conn:
            initialize_paper_db(conn)
            prediction_id = record_prediction(conn, result, recorded_at_utc="2026-08-16T12:00:03Z")
            record_outcome(
                conn,
                prediction_id,
                observed_at_utc="2026-09-01T00:00:00Z",
                outcome_source="fixture_resolution",
                complete_basket_payout_usd=result.paper_filled_shares,
            )
            comparison = compare_predictions(conn)[0]
        self.assertEqual(comparison["predicted_net_edge_usd"], result.net_executable_edge_usd)
        self.assertAlmostEqual(comparison["prediction_error_usd"], 0)

    def test_trial_round_robin_evaluates_multiple_events_in_order(self) -> None:
        candidates = negrisk_paper_smoke.CandidateRoundRobin()
        candidates.refresh([{"id": "a", "markets": [1, 2, 3]}, {"id": "b", "markets": [1, 2, 3]}])
        config = ValidationConfig(target_shares=1, min_shares=1, latency_seconds=0)

        def fake_books(_client, event, *, fee_info):
            updated = dict(fee_info)
            updated[f"condition-{event['id']}"] = ({}, None)
            return [event["id"]], updated

        def fake_evaluation(*, event_id, **_kwargs):
            return SimpleNamespace(event_id=event_id)

        observed = []
        fee_info = {}
        with (
            patch.object(negrisk_paper_smoke, "fetch_basket_books", side_effect=fake_books),
            patch.object(negrisk_paper_smoke, "evaluate_basket", side_effect=fake_evaluation),
            patch.object(negrisk_paper_smoke.time, "sleep"),
        ):
            for _ in range(4):
                _event, evaluation, fee_info, errors = negrisk_paper_smoke.evaluate_next_candidate(
                    object(), candidates, fee_info=fee_info, config=config, latency_seconds=0
                )
                self.assertEqual(errors, [])
                observed.append(evaluation.event_id)

        self.assertEqual(observed, ["a", "b", "a", "b"])
        self.assertEqual(set(fee_info), {"condition-a", "condition-b"})

    def test_trial_unreadable_event_does_not_block_later_event(self) -> None:
        candidates = negrisk_paper_smoke.CandidateRoundRobin()
        candidates.refresh([{"id": "bad", "markets": [1, 2, 3]}, {"id": "good", "markets": [1, 2, 3]}])

        def fake_books(_client, event, *, fee_info):
            if event["id"] == "bad":
                raise TimeoutError("fixture unreadable")
            return [event["id"]], fee_info

        with (
            patch.object(negrisk_paper_smoke, "fetch_basket_books", side_effect=fake_books),
            patch.object(negrisk_paper_smoke, "evaluate_basket", return_value=SimpleNamespace(event_id="good")),
            patch.object(negrisk_paper_smoke.time, "sleep"),
        ):
            event, evaluation, _fees, errors = negrisk_paper_smoke.evaluate_next_candidate(
                object(),
                candidates,
                fee_info={},
                config=ValidationConfig(target_shares=1, min_shares=1, latency_seconds=0),
                latency_seconds=0,
            )

        self.assertEqual(event["id"], "good")
        self.assertEqual(evaluation.event_id, "good")
        self.assertEqual(errors, [{"event_id": "bad", "error": "TimeoutError: fixture unreadable"}])

    def test_trial_discovery_refresh_introduces_new_eligible_event(self) -> None:
        first = [{"id": "a", "markets": [1, 2, 3]}]
        refreshed = first + [{"id": "new", "markets": [1, 2, 3]}]
        candidates = negrisk_paper_smoke.CandidateRoundRobin()
        with patch.object(negrisk_paper_smoke, "discover_events", side_effect=[first, refreshed]):
            candidates.refresh(negrisk_paper_smoke.bounded_events(object(), limit=100))
            self.assertEqual(candidates.next()["id"], "a")
            candidates.refresh(negrisk_paper_smoke.bounded_events(object(), limit=100))

        self.assertEqual(candidates.event_ids, ["a", "new"])
        self.assertEqual(candidates.next()["id"], "a")
        self.assertEqual(candidates.next()["id"], "new")

    def test_trial_discovery_keeps_eight_leg_bound(self) -> None:
        events = [{"id": "safe", "markets": list(range(8))}, {"id": "large", "markets": list(range(9))}]
        with patch.object(negrisk_paper_smoke, "discover_events", return_value=events):
            result = negrisk_paper_smoke.bounded_events(object(), limit=100)
        self.assertEqual([event["id"] for event in result], ["safe"])

    def test_new_layer_has_no_live_execution_path(self) -> None:
        root = Path(__file__).resolve().parents[1]
        files = list((root / "negrisk_economics").glob("*.py")) + [root / "scripts" / "negrisk_paper_smoke.py"]
        forbidden_symbols = {"post", "put", "patch", "delete", "place_order", "create_order", "execute_order", "cancel_order"}
        for path in files:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            attributes = {node.attr.lower() for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
            names = {node.id.lower() for node in ast.walk(tree) if isinstance(node, ast.Name)}
            self.assertFalse(forbidden_symbols & (attributes | names), path)
            self.assertNotIn("/order", source.lower(), path)
        live_tree = ast.parse((root / "negrisk_economics" / "live.py").read_text(encoding="utf-8"))
        methods = [
            keyword.value.value
            for node in ast.walk(live_tree)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg == "method" and isinstance(keyword.value, ast.Constant)
        ]
        self.assertEqual(methods, ["GET"])

if __name__ == "__main__":
    unittest.main()
