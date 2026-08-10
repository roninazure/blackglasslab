from __future__ import annotations

import json
import sqlite3
import unittest

from revenue_poc.config import RevenueConfig
from revenue_poc.reporting import alpha_attribution_report, alpha_leaderboard_report
from revenue_poc.repository import apply_schema
from revenue_poc.service import RevenuePOCService


class AlphaAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(
            """
            CREATE TABLE shadow_forecasts (
              id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, timestamp_utc TEXT NOT NULL,
              venue TEXT NOT NULL, market_id TEXT NOT NULL, question TEXT NOT NULL,
              category TEXT NOT NULL, market_probability REAL NOT NULL,
              model_probability REAL NOT NULL, absolute_edge REAL NOT NULL,
              production_decision TEXT NOT NULL, rejection_reason TEXT,
              time_to_resolution_days REAL, market_end_date TEXT,
              llm_used INTEGER NOT NULL, metadata TEXT NOT NULL
            );
            """
        )
        apply_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _add_forecast(self) -> None:
        metadata = json.dumps({
            "spread": 0.01,
            "market_snapshot": {"best_bid": 0.49, "best_ask": 0.51, "liquidity": 1000},
            "source_metadata": {"source_type": "event_feed", "series": "test-series"},
        })
        self.conn.execute(
            "INSERT INTO shadow_forecasts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (1, "run-1", "2026-08-01T00:00:00+00:00", "polymarket", "m-1",
             "Will m-1 happen?", "politics", 0.50, 0.60, 0.10, "rejected",
             "test", 2.0, "2026-08-20T00:00:00Z", 0, metadata),
        )
        self.conn.commit()

    def test_entry_and_completion_are_immutable_and_reconcile(self) -> None:
        self._add_forecast()
        service = RevenuePOCService(self.conn, RevenueConfig())
        self.assertEqual(service.ingest_shadow_forecasts()["admitted"], 1)
        entry = self.conn.execute(
            "SELECT strategy_id,horizon_bucket,entry_benchmark_source FROM revenue_poc_attribution_entries"
        ).fetchone()
        self.assertEqual(entry, ("revenue_poc", "FAST", "market_probability_normalized_to_side"))
        self.assertTrue(service.mark_position(1, {
            "best_bid": 0.59, "best_ask": 0.61, "quote_timestamp_utc": "2026-08-02T00:00:00Z",
            "bid_depth_usd": 100, "ask_depth_usd": 100, "quote_source": "test",
        }))
        self.assertTrue(service.resolve_position(1, "YES", "2026-08-03T00:00:00Z"))
        report = alpha_attribution_report(self.conn)
        self.assertTrue(report["coverage"]["target_met"])
        self.assertTrue(report["reconciliation"]["within_one_cent"])
        self.assertEqual(report["attributions"][0]["attribution_status"], "COMPLETE")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE revenue_poc_attribution_entries SET category='other'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM revenue_poc_attribution_completions")

    def test_missing_mark_is_explicit_proxy(self) -> None:
        self._add_forecast()
        service = RevenuePOCService(self.conn, RevenueConfig())
        service.ingest_shadow_forecasts()
        self.assertTrue(service.resolve_position(1, "NO", "2026-08-03T00:00:00Z"))
        report = alpha_attribution_report(self.conn)
        self.assertEqual(report["attributions"][0]["attribution_status"], "PARTIAL_PROXY")
        self.assertEqual(report["attributions"][0]["event_alpha_usd"], 0.0)

    def test_leaderboard_labels_unresolved_and_does_not_rank(self) -> None:
        self._add_forecast()
        service = RevenuePOCService(self.conn, RevenueConfig())
        service.ingest_shadow_forecasts()
        report = alpha_leaderboard_report(self.conn)
        self.assertEqual(report["summary"]["resolved_positions"], 0)
        self.assertEqual(report["summary"]["ranked_rows"], 0)
        self.assertEqual(report["leaderboard"][0]["attribution_status"], "unresolved")
        self.assertEqual(report["leaderboard"][0]["sample_size_status"], "unresolved")
        self.assertIsNone(report["leaderboard"][0]["rank"])

    def test_leaderboard_aggregates_realized_metrics(self) -> None:
        for forecast_id in range(1, 7):
            metadata = json.dumps({
                "spread": 0.01,
                "market_snapshot": {"best_bid": 0.49, "best_ask": 0.51, "liquidity": 1000},
                "source_metadata": {"source_type": "event_feed", "series": "test-series"},
            })
            self.conn.execute(
                "INSERT INTO shadow_forecasts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (forecast_id, "run-1", f"2026-08-0{forecast_id}T00:00:00+00:00", "polymarket",
                 f"m-{forecast_id}", f"Will m-{forecast_id} happen?", "politics", 0.50, 0.60,
                 0.10, "rejected", "test", 2.0, "2026-08-20T00:00:00Z", 0, metadata),
            )
        self.conn.commit()
        service = RevenuePOCService(self.conn, RevenueConfig(max_category_positions=6))
        self.assertEqual(service.ingest_shadow_forecasts()["admitted"], 6)
        for position_id, outcome in ((1, "YES"), (2, "YES"), (3, "NO"), (4, "NO"), (5, "YES")):
            self.assertTrue(service.resolve_position(position_id, outcome, "2026-08-10T00:00:00Z"))
        report = alpha_leaderboard_report(self.conn)
        row = report["leaderboard"][0]
        self.assertEqual(row["resolved_positions"], 5)
        self.assertEqual(row["sample_size_status"], "realized")
        self.assertEqual(row["rank"], 1)
        self.assertEqual(row["win_rate"], 0.6)
        self.assertIsNotNone(row["profit_factor"])
        self.assertGreater(row["capital_days"], 0)
        self.assertIsNotNone(row["realized_pnl_per_capital_day"])
        self.assertEqual(report["summary"]["completed_attributions"], 5)


if __name__ == "__main__":
    unittest.main()
