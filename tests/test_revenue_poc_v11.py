from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from llm.claude_client import _system_content
from llm.routing import route_model
from loop_engine.config import LLMBudget, LoopEngineConfig
from market_universe.discovery import discover_markets
from revenue_poc.market_health import is_quarantined, record_market_fetch
from revenue_poc.repository import apply_schema
from revenue_poc.service import RevenuePOCService


class RevenuePOCv11Tests(unittest.TestCase):
    def test_spend_budget_allows_more_than_100_cheap_calls(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = LoopEngineConfig(
                max_llm_calls_per_cycle=500,
                max_skeptic_calls_per_cycle=10,
                max_daily_llm_calls=1000,
                emergency_call_ceiling=1000,
                daily_llm_budget_usd=2.0,
                screening_cost_usd=0.001,
            )
            budget = LLMBudget(
                cfg, Path(td) / "usage.json", now=datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
            )
            calls = 0
            while budget.reserve_primary(model="claude-haiku-4-5-20251001", estimated_cost_usd=0.001, priority=0.9):
                calls += 1
                if calls > 150:
                    break
            self.assertGreater(calls, 100)
            self.assertLessEqual(budget.spent_usd + budget.reserved_usd, 2.0)

    def test_dollar_budget_stops_further_calls(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = LoopEngineConfig(
                max_llm_calls_per_cycle=20,
                max_skeptic_calls_per_cycle=1,
                max_daily_llm_calls=1000,
                emergency_call_ceiling=1000,
                daily_llm_budget_usd=2.0,
                screening_cost_usd=0.5,
            )
            budget = LLMBudget(
                cfg, Path(td) / "usage.json", now=datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
            )
            self.assertTrue(all(
                budget.reserve_primary(model="claude-haiku-4-5-20251001", estimated_cost_usd=0.5, priority=1.0)
                for _ in range(4)
            ))
            self.assertFalse(
                budget.reserve_primary(model="claude-haiku-4-5-20251001", estimated_cost_usd=0.5, priority=1.0)
            )
            self.assertEqual(budget.skipped_by_cost, 1)

    def test_model_routing_and_cache_metadata(self) -> None:
        cfg = LoopEngineConfig()
        self.assertEqual(route_model(opportunity_score=40, config=cfg).tier, "screening")
        self.assertEqual(route_model(opportunity_score=90, config=cfg).tier, "finalist")
        self.assertEqual(route_model(opportunity_score=40, skeptic=True, config=cfg).tier, "skeptic")
        payload = _system_content("stable shared prompt")
        self.assertIsInstance(payload, list)
        self.assertEqual(payload[0]["cache_control"]["type"], "ephemeral")

    def test_discovery_deduplicates_and_finds_outside_fixed_watchlist(self) -> None:
        markets = [
            {"id": "fixed", "slug": "fixed", "question": "Will the FOMC cut rates?", "active": True, "closed": False, "liquidity": 100000, "volume": 1000000, "bestBid": .49, "bestAsk": .51, "outcomes": '["Yes","No"]', "endDate": "2026-12-01T00:00:00Z"},
            {"id": "outside", "slug": "outside", "question": "Will Bitcoin exceed $100k?", "active": True, "closed": False, "liquidity": 200000, "volume": 2000000, "bestBid": .49, "bestAsk": .51, "outcomes": '["Yes","No"]', "endDate": "2026-12-01T00:00:00Z"},
            {"id": "outside", "slug": "outside", "question": "duplicate", "active": True, "closed": False},
            {"id": "closed", "slug": "closed", "question": "closed", "active": True, "closed": True},
        ]
        result = discover_markets(markets, fixed_watchlist=["fixed"], shortlist_size=10)
        self.assertEqual(result["scan"]["total_discovered"], 3)
        self.assertEqual(result["scan"]["opportunities_outside_fixed_watchlist"], 1)
        self.assertEqual(result["scan"]["valid_contracts"], 2)

    def test_stale_market_quarantines_on_third_failure(self) -> None:
        conn = sqlite3.connect(":memory:")
        apply_schema(conn)
        for _ in range(2):
            self.assertEqual(record_market_fetch(conn, venue="polymarket", market_id="dead", ok=False, reason="HTTP 404"), "FAILING")
        self.assertEqual(record_market_fetch(conn, venue="polymarket", market_id="dead", ok=False, reason="HTTP 404"), "QUARANTINED")
        self.assertTrue(is_quarantined(conn, venue="polymarket", market_id="dead"))
        self.assertEqual(conn.execute("SELECT consecutive_failures FROM revenue_poc_market_health WHERE market_id='dead'").fetchone()[0], 3)
        conn.close()

    def test_threshold_experiments_preserve_active_two_percent_lane(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE paper_trades (id INTEGER PRIMARY KEY, market_id TEXT, status TEXT)")
        conn.execute("""CREATE TABLE shadow_forecasts (
            id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, timestamp_utc TEXT NOT NULL,
            venue TEXT NOT NULL, market_id TEXT NOT NULL, question TEXT NOT NULL,
            category TEXT NOT NULL, market_probability REAL NOT NULL, model_probability REAL NOT NULL,
            absolute_edge REAL NOT NULL, production_decision TEXT NOT NULL, rejection_reason TEXT,
            time_to_resolution_days REAL, market_end_date TEXT, llm_used INTEGER NOT NULL, metadata TEXT NOT NULL)""")
        apply_schema(conn)
        metadata = json.dumps({"spread": .01, "market_snapshot": {"best_bid": .49, "best_ask": .51, "liquidity": 50000}})
        conn.execute("INSERT INTO shadow_forecasts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (1, "run-1", "2026-08-05T00:00:00+00:00", "polymarket", "m1", "Will rates fall?", "macro/fed", .5, .55, .05, "candidate_pending_approval", None, 30.0, "2026-12-01T00:00:00Z", 0, metadata))
        conn.commit()
        RevenuePOCService(conn).ingest_shadow_forecasts()
        labels = {row[0] for row in conn.execute("SELECT threshold_label FROM revenue_poc_shadow_thresholds")}
        self.assertEqual(labels, {"0.5%", "1.0%", "1.5%", "2.0%", "2.5%", "3.0%", "adaptive"})
        self.assertEqual(conn.execute("SELECT min_executable_edge FROM revenue_poc_accounts WHERE id=1").fetchone()[0], .02)
        conn.close()


if __name__ == "__main__":
    unittest.main()
