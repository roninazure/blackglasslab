from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from microstructure_snapshots import (
    ensure_microstructure_schema,
    persist_snapshots,
    snapshot_from_market,
)


ROOT = Path(__file__).resolve().parent.parent


class MicrostructureSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        ensure_microstructure_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_snapshot_writes_available_fields_and_derives_midpoint_spread(self) -> None:
        row = snapshot_from_market(
            {
                "id": "market-1",
                "bestBid": "0.41",
                "bestAsk": "0.43",
                "lastTradePrice": "0.42",
                "liquidity": "1200",
                "volume": "3400",
                "endDate": "2026-08-26T00:00:00Z",
                "updatedAt": "2026-08-25T23:00:00Z",
            },
            timestamp_utc="2026-08-25T22:00:00+00:00",
            cycle_id="cycle-1",
            venue="polymarket",
            slug="market-one",
        )
        result = persist_snapshots(self.conn, [row])
        self.assertEqual(result["inserted"], 1)
        saved = self.conn.execute(
            "SELECT timestamp_utc,cycle_id,market_id,slug,best_bid,best_ask,"
            "midpoint,last_trade,spread,liquidity,volume,resolution_timestamp_utc,"
            "time_remaining_hours,source,source_updated_at_utc "
            "FROM market_microstructure_snapshots"
        ).fetchone()
        self.assertEqual(saved[0:4], ("2026-08-25T22:00:00+00:00", "cycle-1", "market-1", "market-one"))
        self.assertEqual(saved[4:8], (0.41, 0.43, 0.42, 0.42))
        self.assertAlmostEqual(saved[8], 0.02)
        self.assertEqual(saved[9:11], (1200.0, 3400.0))
        self.assertEqual(saved[11], "2026-08-26T00:00:00+00:00")
        self.assertEqual(saved[12], 2.0)
        self.assertEqual(saved[13], "polymarket_gamma_market_response")

    def test_unavailable_fields_are_null_and_no_values_are_invented(self) -> None:
        row = snapshot_from_market(
            {"id": "market-2", "liquidity": "bad", "volume": None},
            timestamp_utc="2026-08-25T22:00:00Z",
            cycle_id="cycle-2",
            venue="polymarket",
            slug="market-two",
        )
        persist_snapshots(self.conn, [row])
        saved = self.conn.execute(
            "SELECT best_bid,best_ask,midpoint,last_trade,spread,"
            "bid_executable_depth_usd,ask_executable_depth_usd,liquidity,volume,"
            "resolution_timestamp_utc,time_remaining_hours FROM market_microstructure_snapshots"
        ).fetchone()
        self.assertTrue(all(value is None for value in saved))

    def test_duplicate_cycle_market_is_idempotent(self) -> None:
        row = snapshot_from_market(
            {"id": "market-3", "bestBid": 0.4, "bestAsk": 0.5},
            timestamp_utc="2026-08-25T22:00:00Z",
            cycle_id="cycle-3",
            venue="polymarket",
            slug="market-three",
        )
        self.assertEqual(persist_snapshots(self.conn, [row])["inserted"], 1)
        second = persist_snapshots(self.conn, [row])
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM market_microstructure_snapshots").fetchone()[0], 1)

    def test_migration_works_on_isolated_temporary_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "isolated.sqlite"
            conn = sqlite3.connect(path)
            ensure_microstructure_schema(conn)
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_microstructure_snapshots'"
            ).fetchone())
            conn.close()

    def test_telemetry_failure_returns_error_without_raising(self) -> None:
        conn = sqlite3.connect(":memory:")
        row = snapshot_from_market(
            {"id": "market-4"},
            timestamp_utc="2026-08-25T22:00:00Z",
            cycle_id="cycle-4",
            venue="polymarket",
            slug="market-four",
        )
        result = persist_snapshots(conn, [row])
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["error"], "missing_table")
        conn.close()


if __name__ == "__main__":
    unittest.main()
