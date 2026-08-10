from __future__ import annotations

import json
import sqlite3
import unittest

from revenue_poc.repository import apply_schema
from revenue_poc.velocity import velocity_coverage_report, velocity_shadow_report


class RevenueVelocityShadowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE shadow_forecasts (
              id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, timestamp_utc TEXT NOT NULL,
              venue TEXT NOT NULL, market_id TEXT NOT NULL, question TEXT NOT NULL,
              category TEXT NOT NULL, market_probability REAL NOT NULL,
              model_probability REAL NOT NULL, absolute_edge REAL NOT NULL,
              production_decision TEXT NOT NULL, rejection_reason TEXT,
              time_to_resolution_days REAL, market_end_date TEXT, llm_used INTEGER NOT NULL,
              metadata TEXT NOT NULL
            )
            """
        )
        apply_schema(self.conn)
        self.conn.execute(
            """
            INSERT INTO revenue_poc_discovery_snapshots
            (run_id,timestamp_utc,venue,market_id,status,dynamic_shortlist,
             deterministic_score,metadata)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                "run-1", "2026-08-10T12:00:00+00:00", "polymarket", "short",
                "VALID", 0, 70.0,
                json.dumps({
                    "category": "macro/fed", "liquidity": 50000, "spread": .01,
                    "temporal": {
                        "market_resolution_date": "2026-08-20T00:00:00Z",
                        "time_remaining_hours": 240,
                    },
                    "scoring_components": {"raw": {"existing_exposure": False}},
                }),
            ),
        )
        self.conn.execute(
            """
            INSERT INTO revenue_poc_discovery_snapshots
            (run_id,timestamp_utc,venue,market_id,status,dynamic_shortlist,
             deterministic_score,metadata)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                "run-1", "2026-08-10T12:00:00+00:00", "polymarket", "long",
                "VALID", 1, 95.0,
                json.dumps({
                    "category": "crypto", "liquidity": 100000, "spread": .01,
                    "temporal": {
                        "market_resolution_date": "2026-12-31T00:00:00Z",
                        "time_remaining_hours": 3408,
                    },
                }),
            ),
        )
        self.conn.execute(
            """
            INSERT INTO revenue_poc_evaluations
            (evaluation_key,state_fingerprint,run_id,timestamp_utc,venue,market_id,
             question,category,model_probability,market_probability,executable_bid,
             executable_ask,spread,depth_usd,depth_source,quote_timestamp_utc,
             quote_timestamp_source,bid_source,ask_source,fee_source,side,entry_price,
             raw_edge,executable_edge,expected_value_usd,fee_usd,slippage_usd,
             spread_cost_usd,capital_required_usd,expected_holding_days,fixed_threshold,
             adaptive_threshold,adaptive_qualifies,llm_used,production_decision,metadata)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "eval-short", "state-short", "run-1", "2026-08-10T12:00:00+00:00",
                "polymarket", "short", "Short market?", "macro/fed", .60, .50,
                .49, .51, .02, 50000, "venue", "2026-08-10T12:00:00+00:00", "venue",
                "venue", "venue", "venue", "YES", .51, .10, .08, 4.0, .1, .02,
                .1, 25.0, 10.0, .02, .02, 1, 1, "rejected", "{}",
            ),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_velocity_ranks_net_ev_per_capital_day_without_writes(self) -> None:
        before = self.conn.execute(
            "SELECT COUNT(*) FROM revenue_poc_discovery_snapshots"
        ).fetchone()[0]
        report = velocity_shadow_report(self.conn)
        self.assertTrue(report["read_only"])
        self.assertEqual(report["universe"]["valid_discovered_opportunities"], 2)
        self.assertEqual(report["universe"]["evaluated_valid_opportunities"], 1)
        self.assertEqual(report["top_velocity"][0]["market_id"], "short")
        self.assertAlmostEqual(report["top_velocity"][0]["ev_per_capital_day"], .016, places=6)
        self.assertEqual(report["candidate_counts_by_horizon"]["WEEKLY"]["evaluated"], 1)
        self.assertEqual(report["api_cost_impact"]["incremental_api_calls"], 0)
        after = self.conn.execute(
            "SELECT COUNT(*) FROM revenue_poc_discovery_snapshots"
        ).fetchone()[0]
        self.assertEqual(before, after)

    def test_coverage_report_separates_unevaluated_markets(self) -> None:
        report = velocity_coverage_report(self.conn)
        self.assertEqual(report["horizon_summary"]["WEEKLY"]["valid_markets"], 1)
        self.assertEqual(report["horizon_summary"]["WEEKLY"]["evaluated_markets"], 1)
        self.assertEqual(report["horizon_summary"]["LONG"]["valid_markets"], 1)
        self.assertEqual(report["horizon_summary"]["LONG"]["unevaluated_markets"], 1)
        self.assertEqual(report["horizon_summary"]["LONG"]["coverage_pct"], 0.0)
        self.assertEqual(report["excluded_before_evaluation"]["count"], 1)
        self.assertEqual(report["short_horizon_economic_justification"]["FAST_WEEKLY_evaluated"], 1)


if __name__ == "__main__":
    unittest.main()
