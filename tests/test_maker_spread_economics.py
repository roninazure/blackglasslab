from __future__ import annotations

import ast
import sqlite3
import unittest
from pathlib import Path

from maker_spread_economics import (
    PublicTrade,
    MakerFeeMetadata,
    MakerValidationConfig,
    QuoteSnapshot,
    combine_fill_evidence,
    conservative_fill_probability,
    evaluate_maker_quote,
    fee_metadata_from_venue,
    hypothetical_quotes,
    infer_side_fill_evidence,
    initialize_paper_db,
    record_followup,
    record_hypothetical_quote,
    record_prediction,
)


AUTHORITATIVE = MakerFeeMetadata(
    rate=0.04,
    exponent=1.0,
    taker_only=True,
    rebate_rate=0.0,
    source="fixture_schedule",
    authoritative=True,
)


def quote(
    at: str,
    *,
    bid: float = 0.49,
    ask: float = 0.51,
    bid_size: float = 10.0,
    ask_size: float = 10.0,
) -> QuoteSnapshot:
    return QuoteSnapshot("market", "token", at, bid, ask, bid_size, ask_size)


class MakerSpreadEconomicsTests(unittest.TestCase):
    def evaluate(
        self,
        *,
        signal: QuoteSnapshot | None = None,
        activation: QuoteSnapshot | None = None,
        post: QuoteSnapshot | None = None,
        metadata: MakerFeeMetadata = AUTHORITATIVE,
    ):
        return evaluate_maker_quote(
            signal_quote=signal or quote("2026-08-16T12:00:00Z"),
            activation_quote=activation or quote("2026-08-16T12:00:01Z"),
            post_quote=post or quote("2026-08-16T12:00:03Z"),
            fee_metadata=metadata,
            config=MakerValidationConfig(target_shares=5, latency_seconds=1),
        )

    def test_positive_spread_becomes_negative_after_adverse_selection(self) -> None:
        result = self.evaluate(post=quote("2026-08-16T12:00:03Z", bid=0.52, ask=0.54))
        self.assertAlmostEqual(result.captured_spread_usd, 0.10)
        self.assertAlmostEqual(result.adverse_selection_cost_usd, 0.15)
        self.assertAlmostEqual(result.conditional_maker_edge_usd, -0.05)
        self.assertEqual(result.status, "REJECTED_CONDITIONAL_MAKER_EDGE")

    def test_rebate_improves_conditional_edge_by_fee_curve_rebate(self) -> None:
        without = self.evaluate()
        with_rebate = self.evaluate(
            metadata=MakerFeeMetadata(0.04, 1.0, True, 0.25, "fixture_schedule", True)
        )
        expected_rebate = 5 * 0.04 * ((0.49 * 0.51) + (0.51 * 0.49)) * 0.25
        self.assertAlmostEqual(with_rebate.maker_rebate_usd, expected_rebate)
        self.assertAlmostEqual(
            with_rebate.conditional_maker_edge_usd - without.conditional_maker_edge_usd,
            expected_rebate,
        )

    def test_missing_rebate_metadata_remains_unknown(self) -> None:
        metadata = fee_metadata_from_venue(
            {"feeSchedule": {"rate": 0.04, "exponent": 1, "takerOnly": True}}
        )
        result = self.evaluate(metadata=metadata)
        self.assertFalse(metadata.calculable)
        self.assertEqual(result.status, "UNKNOWN_MAKER_REBATE_ECONOMICS")
        self.assertIsNone(result.conditional_maker_edge_usd)

    def test_latency_invalidates_stale_quote(self) -> None:
        result = self.evaluate(
            activation=quote("2026-08-16T12:00:01Z", bid=0.48, ask=0.52),
            post=quote("2026-08-16T12:00:03Z", bid=0.48, ask=0.52),
        )
        self.assertFalse(result.quote_survived_latency)
        self.assertEqual(result.status, "REJECTED_STALE_QUOTE_AFTER_LATENCY")
        self.assertIsNone(result.conditional_maker_edge_usd)

    def test_no_fill_evidence_preserves_unknown_expected_edge(self) -> None:
        result = self.evaluate()
        self.assertGreater(result.conditional_maker_edge_usd, 0)
        self.assertEqual(result.status, "CONDITIONAL_EDGE_FILL_UNKNOWN")
        self.assertIsNone(result.fill_probability)
        self.assertIsNone(result.fill_adjusted_expected_edge_usd)
        self.assertEqual(result.paper_filled_shares, 0)

    def test_quote_lifetime_and_displayed_depth_are_observed(self) -> None:
        result = self.evaluate(
            activation=quote("2026-08-16T12:00:01Z", bid_size=8, ask_size=6),
            post=quote("2026-08-16T12:00:04Z", bid_size=7, ask_size=5),
        )
        self.assertTrue(result.quote_persisted_through_observation)
        self.assertEqual(result.quote_lifetime_lower_bound_seconds, 3)
        self.assertIsNone(result.quote_lifetime_upper_bound_seconds)
        self.assertAlmostEqual(result.displayed_bid_depth_usd, 0.49 * 8)
        self.assertAlmostEqual(result.displayed_ask_depth_usd, 0.51 * 6)

    def test_edge_calculation_is_deterministic(self) -> None:
        one = self.evaluate(post=quote("2026-08-16T12:00:03Z", bid=0.50, ask=0.52))
        two = self.evaluate(post=quote("2026-08-16T12:00:03Z", bid=0.50, ask=0.52))
        self.assertEqual(one, two)
        self.assertAlmostEqual(
            one.conditional_maker_edge_usd,
            one.captured_spread_usd
            + one.maker_rebate_usd
            - one.adverse_selection_cost_usd
            - one.applicable_maker_fees_usd,
        )

    def test_paper_record_supports_later_fill_and_movement_evidence(self) -> None:
        result = self.evaluate()
        with sqlite3.connect(":memory:") as conn:
            initialize_paper_db(conn)
            prediction_id = record_prediction(conn, result, recorded_at_utc="2026-08-16T12:00:04Z")
            followup_id = record_followup(
                conn,
                prediction_id,
                observed_at_utc="2026-08-16T12:01:00Z",
                evidence_source="paper_quote_reconciliation",
                maker_fill_observed=None,
                later_midpoint=0.52,
                realized_conditional_edge_usd=None,
                evidence={"queue_position": "UNKNOWN"},
            )
            row = conn.execute(
                "SELECT p.execution_mode,p.paper_filled_shares,f.maker_fill_observed,f.later_midpoint "
                "FROM paper_maker_predictions p JOIN paper_maker_followups f ON f.prediction_id=p.id "
                "WHERE f.id=?",
                (followup_id,),
            ).fetchone()
        self.assertEqual(row, ("PAPER_ONLY", 0.0, None, 0.52))

    def fill_quotes(self):
        return hypothetical_quotes(
            market_id="market",
            token_id="token",
            bid=0.49,
            ask=0.51,
            size_shares=5,
            signaled_at_utc="2026-08-16T12:00:00Z",
            eligible_from_utc="2026-08-16T12:00:01Z",
            bid_depth_shares=10,
            ask_depth_shares=12,
        )

    def test_price_touch_alone_does_not_equal_fill(self) -> None:
        bid, _ask = self.fill_quotes()
        evidence = infer_side_fill_evidence(
            bid,
            trades=(),
            final_depth_at_quote_shares=10,
            final_midpoint=0.49,
            followup_available=True,
        )
        self.assertEqual(evidence.state, "NO_FILL_EVIDENCE")
        self.assertTrue(evidence.midpoint_touched_quote)
        self.assertTrue(any("touch alone" in reason for reason in evidence.reasons))

    def test_trade_through_produces_stronger_fill_evidence(self) -> None:
        bid, _ask = self.fill_quotes()
        evidence = infer_side_fill_evidence(
            bid,
            trades=(PublicTrade("token", "SELL", 0.48, 1, "2026-08-16T12:00:02Z"),),
            final_depth_at_quote_shares=0,
            final_midpoint=0.48,
            followup_available=True,
        )
        self.assertEqual(evidence.state, "TRADED_THROUGH")

    def test_displayed_queue_ahead_blocks_optimistic_inference(self) -> None:
        bid, _ask = self.fill_quotes()
        evidence = infer_side_fill_evidence(
            bid,
            trades=(PublicTrade("token", "SELL", 0.49, 10, "2026-08-16T12:00:02Z"),),
            final_depth_at_quote_shares=0,
            final_midpoint=0.49,
            followup_available=True,
        )
        self.assertEqual(evidence.state, "POSSIBLE_FILL")
        self.assertLess(evidence.trades_at_quote_shares, bid.displayed_depth_ahead_shares + bid.size_shares)

    def test_one_sided_strong_evidence_creates_inventory_risk(self) -> None:
        bid_quote, ask_quote = self.fill_quotes()
        bid = infer_side_fill_evidence(
            bid_quote,
            trades=(PublicTrade("token", "SELL", 0.48, 1, "2026-08-16T12:00:02Z"),),
            final_depth_at_quote_shares=0,
            final_midpoint=0.48,
            followup_available=True,
        )
        ask = infer_side_fill_evidence(
            ask_quote,
            trades=(),
            final_depth_at_quote_shares=12,
            final_midpoint=0.48,
            followup_available=True,
        )
        combined = combine_fill_evidence(
            bid, ask, quote_size_shares=5, observed_trade_count=1, followup_window_seconds=2
        )
        self.assertEqual(combined.two_sided_completion_state, "NO_TWO_SIDED_FILL_EVIDENCE")
        self.assertEqual(combined.inventory_risk_state, "ONE_SIDED_LONG_RISK")
        self.assertEqual(combined.hypothetical_inventory_shares, 5)

    def test_both_sides_independently_require_strong_evidence(self) -> None:
        bid_quote, ask_quote = self.fill_quotes()
        trades = (
            PublicTrade("token", "SELL", 0.48, 1, "2026-08-16T12:00:02Z"),
            PublicTrade("token", "BUY", 0.52, 1, "2026-08-16T12:00:02Z"),
        )
        bid = infer_side_fill_evidence(
            bid_quote, trades=trades, final_depth_at_quote_shares=0, final_midpoint=0.50, followup_available=True
        )
        ask = infer_side_fill_evidence(
            ask_quote, trades=trades, final_depth_at_quote_shares=0, final_midpoint=0.50, followup_available=True
        )
        combined = combine_fill_evidence(
            bid, ask, quote_size_shares=5, observed_trade_count=2, followup_window_seconds=2
        )
        self.assertEqual(combined.two_sided_completion_state, "TWO_SIDED_PROBABLE")
        self.assertEqual(combined.hypothetical_inventory_shares, 0)

    def test_missing_followup_data_remains_unknown(self) -> None:
        bid, _ask = self.fill_quotes()
        evidence = infer_side_fill_evidence(
            bid,
            trades=(),
            final_depth_at_quote_shares=None,
            final_midpoint=None,
            followup_available=False,
        )
        self.assertEqual(evidence.state, "UNKNOWN")

    def test_conservative_probability_uses_wilson_bound_after_minimum_trials(self) -> None:
        sparse, sparse_status = conservative_fill_probability(
            complete_evidence_trials=9, observed_trials=29
        )
        measured, measured_status = conservative_fill_probability(
            complete_evidence_trials=30, observed_trials=30
        )
        self.assertIsNone(sparse)
        self.assertEqual(sparse_status, "UNKNOWN_INSUFFICIENT_EMPIRICAL_TRIALS")
        self.assertGreater(measured, 0)
        self.assertLess(measured, 1)
        self.assertIn("WILSON_LOWER_BOUND", measured_status)

    def test_followup_and_hypothetical_quotes_link_to_prediction(self) -> None:
        result = self.evaluate()
        bid_quote, ask_quote = self.fill_quotes()
        bid = infer_side_fill_evidence(
            bid_quote, trades=(), final_depth_at_quote_shares=10, final_midpoint=0.50, followup_available=True
        )
        ask = infer_side_fill_evidence(
            ask_quote, trades=(), final_depth_at_quote_shares=12, final_midpoint=0.50, followup_available=True
        )
        followup = combine_fill_evidence(
            bid, ask, quote_size_shares=5, observed_trade_count=0, followup_window_seconds=2
        )
        with sqlite3.connect(":memory:") as conn:
            initialize_paper_db(conn)
            prediction_id = record_prediction(conn, result)
            record_hypothetical_quote(conn, prediction_id, bid_quote)
            record_hypothetical_quote(conn, prediction_id, ask_quote)
            record_followup(
                conn,
                prediction_id,
                observed_at_utc="2026-08-16T12:00:03Z",
                evidence_source=followup.evidence_source,
                maker_fill_observed=None,
                later_midpoint=0.50,
                realized_conditional_edge_usd=None,
                fill_followup=followup,
                conservative_fill_probability_status="UNKNOWN_INSUFFICIENT_EMPIRICAL_TRIALS",
            )
            quote_rows = conn.execute(
                "SELECT side,quote_price,displayed_depth_ahead_shares FROM paper_maker_hypothetical_quotes "
                "WHERE prediction_id=? ORDER BY side",
                (prediction_id,),
            ).fetchall()
            followup_row = conn.execute(
                "SELECT bid_fill_evidence_state,ask_fill_evidence_state,two_sided_completion_state,"
                "maker_fill_observed FROM paper_maker_followups WHERE prediction_id=?",
                (prediction_id,),
            ).fetchone()
        self.assertEqual(quote_rows, [("ASK", 0.51, 12.0), ("BID", 0.49, 10.0)])
        self.assertEqual(
            followup_row,
            ("NO_FILL_EVIDENCE", "NO_FILL_EVIDENCE", "NO_TWO_SIDED_FILL_EVIDENCE", None),
        )

    def test_no_live_execution_path_is_reachable(self) -> None:
        root = Path(__file__).resolve().parents[1]
        files = list((root / "maker_spread_economics").glob("*.py")) + [
            root / "scripts" / "maker_spread_paper_smoke.py"
        ]
        forbidden = {
            "post",
            "put",
            "patch",
            "delete",
            "place_order",
            "create_order",
            "execute_order",
            "cancel_order",
        }
        for path in files:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            symbols = {
                node.attr.lower() for node in ast.walk(tree) if isinstance(node, ast.Attribute)
            } | {node.id.lower() for node in ast.walk(tree) if isinstance(node, ast.Name)}
            self.assertFalse(forbidden & symbols, path)
            self.assertNotIn("/order", source.lower(), path)
        live_tree = ast.parse((root / "maker_spread_economics" / "live.py").read_text())
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
    combine_fill_evidence,
    conservative_fill_probability,
    hypothetical_quotes,
    infer_side_fill_evidence,
