from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from loop_engine.shadow import insert_shadow_forecast
from scripts import resolve_paper_trades as resolver


SCHEMA = Path(__file__).resolve().parent.parent / "migrations" / "005_phase3_3_shadow_forecasts.sql"


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    return conn


def add_forecast(conn: sqlite3.Connection, *, run_id: str, market_id: str, slug: str) -> int:
    return insert_shadow_forecast(
        conn,
        {
            "run_id": run_id,
            "market_id": market_id,
            "slug": slug,
            "timestamp_utc": "2026-01-01T00:00:00+00:00",
            "market_probability": 0.4,
            "model_probability": 0.7,
            "side": "YES",
            "metadata": {},
        },
        production_threshold=0.04,
    ).forecast_id


RESOLVED = {"closed": True, "active": False, "outcomes": ["Yes", "No"], "outcomePrices": ["1", "0"]}
UNRESOLVED = {"closed": False, "active": True, "outcomes": ["Yes", "No"], "outcomePrices": ["0.5", "0.5"]}


class ResolverDedupTests(unittest.TestCase):
    def test_repeated_snapshots_use_one_lookup_and_resolve_atomically(self) -> None:
        conn = make_db()
        add_forecast(conn, run_id="run-1", market_id="contract-1", slug="contract-one")
        add_forecast(conn, run_id="run-2", market_id="contract-1", slug="contract-one")
        with mock.patch.object(resolver, "_fetch_shadow_contract", return_value=(RESOLVED, "market_id", 1)) as fetch:
            metrics = resolver.resolve_shadow_forecasts(
                conn, limit=0, timeout_s=1, sleep_s=0, dry_run=False
            )
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(metrics.snapshot_rows_considered, 2)
        self.assertEqual(metrics.unique_contracts_checked, 1)
        self.assertEqual(metrics.resolved_contracts, 1)
        self.assertEqual(metrics.snapshot_rows_resolved, 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM shadow_forecasts WHERE status='OPEN'").fetchone()[0], 0)

    def test_unresolved_contract_has_no_row_by_row_mutation(self) -> None:
        conn = make_db()
        add_forecast(conn, run_id="run-1", market_id="contract-1", slug="contract-one")
        add_forecast(conn, run_id="run-2", market_id="contract-1", slug="contract-one")
        with mock.patch.object(resolver, "_fetch_shadow_contract", return_value=(UNRESOLVED, "slug", 1)) as fetch:
            metrics = resolver.resolve_shadow_forecasts(
                conn, limit=0, timeout_s=1, sleep_s=0, dry_run=False
            )
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(metrics.resolved_contracts, 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM shadow_forecasts WHERE status='OPEN'").fetchone()[0], 2)

    def test_newer_contracts_are_not_starved_by_first_hundred_rows(self) -> None:
        conn = make_db()
        for index in range(105):
            add_forecast(conn, run_id=f"run-{index}", market_id=f"contract-{index}", slug=f"contract-{index}")
        with mock.patch.object(resolver, "_fetch_shadow_contract", return_value=(UNRESOLVED, "slug", 1)) as fetch:
            metrics = resolver.resolve_shadow_forecasts(
                conn, limit=0, timeout_s=1, sleep_s=0, dry_run=True
            )
        self.assertEqual(fetch.call_count, 105)
        self.assertEqual(metrics.unique_contracts_checked, 105)
        self.assertEqual(metrics.snapshot_rows_considered, 105)

    def test_slug_fallback_normalizes_contract_identity(self) -> None:
        conn = make_db()
        add_forecast(conn, run_id="run-1", market_id="Will Foo?", slug="Will Foo?")
        add_forecast(conn, run_id="run-2", market_id="will-foo", slug="will-foo")
        with mock.patch.object(resolver, "_fetch_shadow_contract", return_value=(UNRESOLVED, "slug", 1)) as fetch:
            metrics = resolver.resolve_shadow_forecasts(
                conn, limit=0, timeout_s=1, sleep_s=0, dry_run=True
            )
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(metrics.unique_contracts_checked, 1)


if __name__ == "__main__":
    unittest.main()
