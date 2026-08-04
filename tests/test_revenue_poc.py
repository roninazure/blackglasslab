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
