from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from revenue_poc.config import RevenueConfig
from revenue_poc.economics import adaptive_threshold, evaluate_execution
from revenue_poc.reporting import portfolio_dashboard
from revenue_poc.repository import apply_schema, downgrade_schema
from revenue_poc.service import RevenuePOCService
from revenue_poc.venue import quote_from_market_and_book, resolved_outcome, yes_token_id


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE paper_trades (id INTEGER PRIMARY KEY, market_id TEXT, status TEXT);
        INSERT INTO paper_trades VALUES (1,'legacy','OPEN');
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
    return conn


def _forecast(conn: sqlite3.Connection, forecast_id: int, *, market: str, category: str = "crypto", model: float = 0.60, market_p: float = 0.50, timestamp: str = "2026-08-04T00:00:00+00:00") -> None:
    metadata = json.dumps(
        {
            "spread": 0.01,
            "market_snapshot": {"best_bid": 0.495, "best_ask": 0.505, "liquidity": 50_000},
        }
    )
    conn.execute(
        "INSERT INTO shadow_forecasts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (forecast_id, f"run-{forecast_id}", timestamp, "polymarket", market, f"Will {market}?", category, market_p, model, abs(model-market_p), "rejected", "min_edge_abs", 30.0, "2030-01-01T00:00:00Z", 1, metadata),
    )
    conn.commit()


class RevenuePOCTests(unittest.TestCase):
    def test_executable_economics_uses_ask_not_midpoint(self) -> None:
        economics = evaluate_execution(
            model_probability=0.55, market_probability=0.50, best_bid=0.49,
            best_ask=0.51, stake_usd=25, depth_usd=1000, slippage_bps=10,
        )
        self.assertEqual(economics.entry_price, 0.51)
        self.assertLess(economics.executable_edge, economics.raw_edge)
        self.assertGreater(economics.expected_value_usd, 0)
        self.assertGreater(adaptive_threshold(spread=0.02, depth_usd=1000, holding_days=365), 0.02)

    def test_independent_ledger_preserves_legacy_and_auto_admits(self) -> None:
        conn = _database()
        _forecast(conn, 1, market="market-1")
        result = RevenuePOCService(conn, RevenueConfig()).ingest_shadow_forecasts()
        self.assertEqual(result["admitted"], 1)
        self.assertEqual(conn.execute("SELECT * FROM paper_trades").fetchall(), [(1, "legacy", "OPEN")])
        position = conn.execute("SELECT size_usd,status FROM revenue_poc_positions").fetchone()
        self.assertEqual(position, (25.0, "OPEN"))
        dashboard = portfolio_dashboard(conn)
        self.assertEqual(dashboard["portfolio"]["starting_balance_usd"], 1000.0)
        self.assertEqual(dashboard["portfolio"]["deployed_capital_usd"], 25.0)
        conn.close()

    def test_cache_duplicate_and_one_position_per_contract(self) -> None:
        conn = _database()
        _forecast(conn, 1, market="same")
        _forecast(conn, 2, market="same", timestamp="2026-08-04T01:00:00+00:00")
        service = RevenuePOCService(conn, RevenueConfig())
        first = service.ingest_shadow_forecasts()
        second = service.ingest_shadow_forecasts()
        self.assertEqual(first["cache_hits"], 1)
        self.assertEqual(second["evaluated"], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM revenue_poc_positions").fetchone()[0], 1)
        self.assertGreaterEqual(conn.execute("SELECT SUM(calls_avoided) FROM revenue_poc_api_daily").fetchone()[0], 2)
        conn.close()

    def test_api_budget_guard(self) -> None:
        conn = _database()
        service = RevenuePOCService(conn, RevenueConfig(daily_api_budget_usd=2.0))
        service.initialize()
        self.assertTrue(service.can_spend_api("2026-08-04", 2.0))
        with conn:
            service._api_increment("2026-08-04", estimated_cost_usd=1.75)
        self.assertTrue(service.can_spend_api("2026-08-04", 0.25))
        self.assertFalse(service.can_spend_api("2026-08-04", 0.26))

    def test_portfolio_and_category_exposure_limits(self) -> None:
        conn = _database()
        for index in range(1, 9):
            _forecast(conn, index, market=f"m-{index}", category="politics")
        config = RevenueConfig(max_category_positions=5)
        result = RevenuePOCService(conn, config).ingest_shadow_forecasts()
        self.assertEqual(result["admitted"], 5)
        reasons = dict(conn.execute("SELECT reason,COUNT(*) FROM revenue_poc_decisions GROUP BY reason"))
        self.assertEqual(reasons["max_category_exposure"], 3)
        conn.close()

    def test_expired_and_skeptic_rejected_markets_never_deploy_capital(self) -> None:
        conn = _database()
        _forecast(conn, 1, market="expired")
        _forecast(conn, 2, market="challenged")
        conn.execute("UPDATE shadow_forecasts SET market_end_date='2026-01-01T00:00:00Z' WHERE id=1")
        conn.execute("UPDATE shadow_forecasts SET rejection_reason='skeptic_reject' WHERE id=2")
        conn.commit()
        service = RevenuePOCService(
            conn,
            RevenueConfig(),
            now=datetime(2026, 8, 4, tzinfo=timezone.utc),
        )
        result = service.ingest_shadow_forecasts()
        self.assertEqual(result["admitted"], 0)
        reasons = {row[0] for row in conn.execute("SELECT reason FROM revenue_poc_decisions")}
        self.assertIn("market_expired", reasons)
        self.assertIn("source_safety_rejection:skeptic_reject", reasons)
        conn.close()

    def test_append_only_and_resolution(self) -> None:
        conn = _database()
        _forecast(conn, 1, market="resolve")
        service = RevenuePOCService(conn, RevenueConfig())
        service.ingest_shadow_forecasts()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE revenue_poc_evaluations SET question='changed'")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM revenue_poc_decisions")
        self.assertTrue(service.resolve_position(1, "YES", "2026-09-01T00:00:00+00:00"))
        self.assertFalse(service.resolve_position(1, "YES"))
        self.assertGreater(conn.execute("SELECT realized_pnl_usd FROM revenue_poc_positions").fetchone()[0], 0)
        conn.close()

    def test_mark_resolution_cash_release_and_recycling_are_idempotent(self) -> None:
        conn = _database()
        _forecast(conn, 1, market="lifecycle")
        service = RevenuePOCService(conn, RevenueConfig())
        service.ingest_shadow_forecasts()
        quote = {
            "best_bid": 0.55,
            "best_ask": 0.57,
            "bid_depth_usd": 100.0,
            "ask_depth_usd": 120.0,
            "depth_source": "fixture_order_book",
            "fee_rate": 0.04,
            "fee_source": "fixture_fee_schedule",
            "quote_timestamp_utc": "2026-08-05T00:00:00+00:00",
            "quote_source": "fixture",
        }
        self.assertTrue(service.mark_position(1, quote))
        self.assertFalse(service.mark_position(1, quote))
        marked = portfolio_dashboard(conn)
        self.assertGreater(marked["performance"]["unrealized_pnl_usd"], 0)
        self.assertEqual(len(marked["current_marks"]), 1)
        self.assertTrue(
            service.resolve_position(
                1,
                "YES",
                "2026-09-01T00:00:00+00:00",
                resolution_fee_usd=0.10,
                resolution_slippage_usd=0.20,
            )
        )
        self.assertFalse(service.resolve_position(1, "YES"))
        self.assertFalse(service.mark_position(1, quote))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE revenue_poc_positions SET realized_pnl_usd=0 WHERE id=1"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM revenue_poc_positions WHERE id=1")
        closed = portfolio_dashboard(conn)
        self.assertEqual(closed["portfolio"]["deployed_capital_usd"], 0.0)
        self.assertGreater(closed["portfolio"]["cash_usd"], 1000.0)
        self.assertEqual(closed["execution"]["realized_fees_usd"], 0.10)
        self.assertEqual(closed["execution"]["realized_slippage_usd"], 0.225)

        _forecast(conn, 2, market="recycled", timestamp="2026-08-05T01:00:00+00:00")
        service.ingest_shadow_forecasts()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM revenue_poc_positions WHERE status='OPEN'").fetchone()[0],
            1,
        )
        conn.close()

    def test_loss_resolution_records_equity_drawdown(self) -> None:
        conn = _database()
        _forecast(conn, 1, market="loss")
        service = RevenuePOCService(conn, RevenueConfig())
        service.ingest_shadow_forecasts()
        self.assertTrue(service.resolve_position(1, "NO", "2026-09-01T00:00:00+00:00"))
        dashboard = portfolio_dashboard(conn)
        self.assertLess(dashboard["performance"]["realized_pnl_usd"], 0)
        self.assertGreater(dashboard["performance"]["maximum_drawdown_usd"], 0)
        conn.close()

    def test_execution_sources_and_historical_api_unknowns_are_explicit(self) -> None:
        conn = _database()
        _forecast(conn, 1, market="sources")
        service = RevenuePOCService(conn, RevenueConfig())
        service.ingest_shadow_forecasts()
        sources = conn.execute(
            "SELECT bid_source,ask_source,depth_source,fee_source,quote_timestamp_source FROM revenue_poc_evaluations"
        ).fetchone()
        self.assertEqual(sources[0:2], ("venue_top_of_book", "venue_top_of_book"))
        self.assertEqual(sources[2], "liquidity_proxy_assumption")
        self.assertEqual(sources[3], "configured_fee_bps_assumption")
        self.assertEqual(sources[4], "forecast_timestamp_assumption")
        dashboard = portfolio_dashboard(conn)
        self.assertIsNone(dashboard["api"]["known_api_spend_usd"])
        self.assertEqual(dashboard["api"]["unknown_cost_calls"], 1)
        self.assertIsNone(dashboard["api"]["remaining_daily_budget_usd"])
        self.assertIsNone(service.remaining_api_budget("2026-08-04"))
        self.assertFalse(service.can_spend_api("2026-08-04", 0.01))
        conn.close()

    def test_observed_api_telemetry_preserves_tokens_and_cost(self) -> None:
        conn = _database()
        _forecast(conn, 1, market="metered")
        usage = {
            "operation": "forecast",
            "model": "claude-haiku-4-5-20251001",
            "input_tokens": 1000,
            "output_tokens": 100,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 30,
            "estimated_cost_usd": 0.001528,
            "pricing_source": "anthropic_public_pricing_2026-08-04",
        }
        conn.execute(
            "UPDATE shadow_forecasts SET metadata=json_set(metadata, '$.anthropic_usage', json(?)) WHERE id=1",
            (json.dumps([usage]),),
        )
        conn.commit()
        service = RevenuePOCService(conn, RevenueConfig())
        service.ingest_shadow_forecasts()
        api_call = conn.execute(
            "SELECT model,input_tokens,output_tokens,cache_creation_input_tokens,"
            "cache_read_input_tokens,estimated_cost_usd,telemetry_status "
            "FROM revenue_poc_api_calls"
        ).fetchone()
        self.assertEqual(api_call[:5], ("claude-haiku-4-5-20251001", 1000, 100, 20, 30))
        self.assertAlmostEqual(api_call[5], 0.001528)
        self.assertEqual(api_call[6], "observed")
        self.assertAlmostEqual(service.remaining_api_budget("2026-08-04"), 1.998472)
        conn.close()

    def test_venue_order_book_quote_and_resolution_parsing(self) -> None:
        market = {
            "outcomes": '["Yes","No"]',
            "clobTokenIds": '["yes-token","no-token"]',
            "feesEnabled": False,
            "closed": True,
            "outcomePrices": '["1","0"]',
        }
        book = {
            "timestamp": "1785859200000",
            "bids": [{"price": "0.49", "size": "100"}],
            "asks": [{"price": "0.51", "size": "80"}],
        }
        self.assertEqual(yes_token_id(market), "yes-token")
        quote = quote_from_market_and_book(market, book, category="politics")
        self.assertEqual(quote["best_bid"], 0.49)
        self.assertEqual(quote["ask_depth_usd"], 40.8)
        self.assertEqual(quote["fee_source"], "venue_market_fee_flag")
        self.assertFalse(quote["assumptions"]["fee_rate_assumed"])
        self.assertEqual(resolved_outcome(market), "YES")

    def test_upgrade_downgrade_preserves_legacy_table(self) -> None:
        conn = _database()
        apply_schema(conn)
        downgrade_schema(conn)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("paper_trades", tables)
        self.assertNotIn("revenue_poc_positions", tables)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0], 1)
        conn.close()

    def test_copied_file_database(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "copy.sqlite"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE shadow_forecasts (id INTEGER PRIMARY KEY)")
            conn.commit()
            before = path.stat().st_size
            conn.close()
            self.assertGreater(before, 0)


if __name__ == "__main__":
    unittest.main()
