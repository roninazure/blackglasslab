from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from revenue_poc.config import RevenueConfig
from revenue_poc.replay import ReplayPolicy, baseline_policy, format_report, open_readonly, replay
from revenue_poc.repository import apply_schema


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(path: Path, *, execution_failure_only: bool = False) -> None:
    conn = sqlite3.connect(path)
    # The production evaluation table retains an optional source FK.
    conn.execute("CREATE TABLE shadow_forecasts (id INTEGER PRIMARY KEY)")
    apply_schema(conn)
    config = RevenueConfig(
        starting_balance_usd=100, position_size_usd=25, max_open_positions=3,
        max_capital_deployed_usd=50, max_category_positions=1, min_executable_edge=.02,
    )
    conn.execute(
        "INSERT INTO revenue_poc_accounts VALUES (1,?,?,?,?,?,?,?,?,?)",
        ("2026-01-01T00:00:00+00:00", 100, 25, 3, 50, 1, .02, 2, json.dumps(config.as_dict())),
    )
    values = [
        # First is admitted; the second is the same immutable contract.
        ("a", "btc-a", "Will Bitcoin reach $100,000 in August 2026?", "crypto", .08, 8),
        ("b", "btc-a", "Will Bitcoin reach $101,000 in August 2026?", "crypto", .07, 8),
        # Different contract but the same BTC/August threshold theme is blocked.
        ("c", "btc-c", "Will Bitcoin reach $102,000 in August 2026?", "crypto", .06, 8),
        ("d", "fed", "Will the Fed cut rates?", "macro/fed", .05, 30),
        ("e", "low", "Will inflation fall?", "macro/econ", .015, 30),
    ]
    if execution_failure_only:
        values = [
            # This passes both the 2% and 3% edge threshold, but its
            # persisted validation failure makes it hard ineligible.
            ("a", "invalid", "Will Bitcoin reach $100,000 in August 2026?", "crypto", .03, 8),
            ("b", "low", "Will inflation fall?", "macro/econ", .015, 30),
        ]
    for index, (key, market, question, category, edge, holding) in enumerate(values, 1):
        conn.execute(
            """INSERT INTO revenue_poc_evaluations
               (evaluation_key,state_fingerprint,run_id,timestamp_utc,venue,market_id,question,category,
                model_probability,market_probability,executable_bid,executable_ask,spread,depth_usd,depth_source,
                quote_timestamp_utc,quote_timestamp_source,bid_source,ask_source,fee_source,side,entry_price,raw_edge,
                executable_edge,expected_value_usd,fee_usd,slippage_usd,spread_cost_usd,capital_required_usd,
                expected_holding_days,fixed_threshold,adaptive_threshold,adaptive_qualifies,llm_used,production_decision,metadata)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, key, "run", f"2026-02-0{index}T00:00:00+00:00", "polymarket", market, question, category,
             .6, .5, .49, .51, .02, 1000, "fixture", "2026-02-01T00:00:00+00:00", "fixture", "fixture", "fixture", "fixture",
             "YES", .51, edge + .01, edge, edge * 25, 0, 0, .25, 25, holding, .02, .02, 1, 0, "fixture", "{}"),
        )
    failure_id = 1 if execution_failure_only else 5
    conn.execute(
        "INSERT INTO revenue_poc_decisions (evaluation_id,timestamp_utc,decision,reason,details) VALUES (?,?,'REJECT','execution_validation_failed:fixture','{}')",
        (failure_id, "2026-02-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()


class RevenueReplayTests(unittest.TestCase):
    def test_readonly_baseline_reproduces_fixture_and_preserves_db(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite"
            _fixture(path)
            before = _digest(path)
            with open_readonly(path) as conn:
                result = replay(conn, start="2026-02-01T00:00:00+00:00", end="2026-03-01T00:00:00+00:00", policy=baseline_policy(conn))
            self.assertEqual(before, _digest(path))
            self.assertEqual(result["evaluations"], 5)
            self.assertEqual(result["hypothetical_admissions"], 2)
            self.assertEqual(result["rejection_reasons"]["one_position_per_contract"], 1)
            self.assertEqual(result["rejection_reasons"]["max_category_exposure_correlated_theme"], 1)
            self.assertEqual(result["capital_deployed_usd"], 50)

    def test_candidate_is_deterministic_and_applies_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite"
            _fixture(path)
            with open_readonly(path) as conn:
                baseline = baseline_policy(conn)
                candidate = baseline.with_overrides(min_executable_edge=.07, max_capital_deployed_usd=25)
                first = replay(conn, start="2026-02-01T00:00:00+00:00", end="2026-03-01T00:00:00+00:00", policy=candidate)
                second = replay(conn, start="2026-02-01T00:00:00+00:00", end="2026-03-01T00:00:00+00:00", policy=candidate)
            self.assertEqual(first, second)
            self.assertEqual(first["hypothetical_admissions"], 1)
            self.assertEqual(first["rejection_reasons"]["max_capital_deployed"], 1)
            self.assertEqual(first["execution_failures"], {"execution_validation_failed:fixture": 1})

    def test_execution_validation_failure_is_never_hypothetically_admitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite"
            _fixture(path, execution_failure_only=True)
            with open_readonly(path) as conn:
                two_percent = replay(
                    conn, start="2026-02-01T00:00:00+00:00", end="2026-03-01T00:00:00+00:00",
                    policy=baseline_policy(conn),
                )
                three_percent = replay(
                    conn, start="2026-02-01T00:00:00+00:00", end="2026-03-01T00:00:00+00:00",
                    policy=baseline_policy(conn).with_overrides(min_executable_edge=.03),
                )
            for result in (two_percent, three_percent):
                self.assertEqual(result["hypothetical_admissions"], 0)
                self.assertEqual(result["qualifying_valid_execution_opportunities"], 0)
                self.assertEqual(result["execution_invalid_opportunities_excluded_from_simulation"], 1)

    def test_report_separates_historical_actuals_from_current_policy_simulation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite"
            _fixture(path)
            with open_readonly(path) as conn:
                result = replay(conn, start="2026-02-01T00:00:00+00:00", end="2026-03-01T00:00:00+00:00", policy=baseline_policy(conn))
            report = format_report(result, result)
            self.assertIn("Historical actual decisions are persisted outcomes", report)
            self.assertIn("Exact historical-policy reproduction is not claimed", report)


if __name__ == "__main__":
    unittest.main()
