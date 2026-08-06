from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from reporting.discovery_breakdown import build_discovery_breakdown
from reporting.discovery_classification import classify_reporting_class
from revenue_poc.repository import apply_schema, downgrade_schema

ROOT = Path(__file__).resolve().parent.parent


def report_fixture() -> dict[str, object]:
    return {
        "run_id": "infer-test",
        "ts_utc": "2026-08-06T00:00:00+00:00",
        "optimization": {
            "discovery": {
                "total_discovered": 10,
                "valid_contracts": 2,
                "shortlisted": 2,
                "outside_fixed_watchlist": 1,
                "rejected_by_reason": {
                    "inactive": 2,
                    "closed": 1,
                    "banned_market_class": 4,
                    "low_institutional_quality": 1,
                },
            }
        },
        "discovery_shadow": {
            "rows": [
                {"market_id": "banned-1", "status": "REJECTED", "reason": "banned_market_class"},
                {"market_id": "banned-2", "status": "REJECTED", "reason": "banned_market_class"},
                {"market_id": "banned-3", "status": "REJECTED", "reason": "banned_market_class"},
                {"market_id": "banned-4", "status": "REJECTED", "reason": "banned_market_class"},
            ],
            "selected": [
                {"market_id": "short-1", "fixed_watchlist": True},
                {"market_id": "short-2", "fixed_watchlist": False},
            ],
        },
        "markets": [
            {
                "market_id": "short-1",
                "decision": "SKIP",
                "reason": "existing_open_or_pending_position",
            }
        ],
    }


class DiscoveryBreakdownTests(unittest.TestCase):
    def test_funnel_percentages_and_example_truncation(self) -> None:
        data = build_discovery_breakdown(report_fixture(), example_limit=2)
        source = data["discovery_source"]
        self.assertEqual(source["total_market_records_expanded"], 10)
        self.assertEqual(source["total_active"], 8)
        self.assertEqual(data["banned_market_classes"]["generic_banned_market_class"]["count"], 4)
        self.assertEqual(
            len(data["banned_market_classes"]["generic_banned_market_class"]["examples"]), 2
        )
        funnel = {row["stage"]: row for row in data["survivor_funnel"]}
        self.assertEqual(funnel["scored"]["count"], 7)
        self.assertEqual(funnel["scored"]["percentage_of_total_discovered"], 70.0)
        self.assertEqual(funnel["valid_after_policy"]["percentage_of_previous"], 28.57)

    def test_generic_banned_class_reports_limitation(self) -> None:
        data = build_discovery_breakdown(report_fixture())
        self.assertFalse(
            data["banned_market_classes"]["generic_banned_market_class"][
                "subtype_metadata_available"
            ]
        )
        self.assertIn("generic banned_market_class", data["banned_market_class_limitation"])

    def test_source_backed_sports_entertainment_esports_and_unknown(self) -> None:
        cases = (
            ({"event_category": "Sports"}, "sports", "source_metadata"),
            ({"tags": [{"label": "Entertainment"}]}, "entertainment", "source_metadata"),
            ({"event_category": "esports"}, "esports", "source_metadata"),
            ({"event_category": "new-unclassified-type"}, "unknown/other", "source_metadata"),
            ({"event_title": "Sports Final"}, "unknown/other", "unknown_metadata"),
        )
        for metadata, expected, source in cases:
            with self.subTest(metadata=metadata):
                self.assertEqual(classify_reporting_class(metadata), (expected, source))

    def test_mixed_historical_and_source_backed_rows(self) -> None:
        report = report_fixture()
        report["discovery_shadow"]["rows"] = [
            {"market_id": "old", "status": "REJECTED", "reason": "banned_market_class"},
            {
                "market_id": "sports",
                "status": "REJECTED",
                "reason": "banned_market_class",
                "features": {
                    "reporting_class": "sports",
                    "classification_source": "source_metadata",
                    "source_metadata": {"event_category": "sports"},
                },
            },
            {
                "market_id": "esports",
                "status": "REJECTED",
                "reason": "banned_market_class",
                "features": {
                    "reporting_class": "esports",
                    "classification_source": "source_metadata",
                    "source_metadata": {"event_category": "esports"},
                },
            },
        ]
        data = build_discovery_breakdown(report)
        self.assertEqual(data["banned_market_classes"]["sports"]["count"], 1)
        self.assertEqual(data["banned_market_classes"]["esports"]["percentage_of_banned"], 25.0)
        self.assertEqual(data["classification_coverage"]["historical_generic_rows"], 1)
        self.assertEqual(data["classification_coverage"]["scored_rows_with_source_classification"], 2)

    def test_empty_discovery_is_safe(self) -> None:
        data = build_discovery_breakdown({})
        self.assertEqual(data["discovery_source"]["total_market_records_expanded"], 0)
        self.assertEqual(data["survivor_funnel"][0]["count"], 0)
        self.assertEqual(data["banned_market_classes"]["generic_banned_market_class"]["count"], 0)

    def test_revenue_gap_reasons_and_read_only_db(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE revenue_poc_evaluations (
              id INTEGER PRIMARY KEY, market_id TEXT, production_decision TEXT,
              production_rejection_reason TEXT, run_id TEXT
            );
            CREATE TABLE revenue_poc_decisions (
              evaluation_id INTEGER, decision TEXT
            );
            """
        )
        before = conn.total_changes
        data = build_discovery_breakdown(report_fixture(), conn=conn)
        self.assertEqual(conn.total_changes, before)
        self.assertEqual(
            data["shortlist_to_revenue_gap"]["groups"]["outside_fixed_watchlist"]["count"], 1
        )
        self.assertEqual(
            data["shortlist_to_revenue_gap"]["groups"]["duplicate_position"]["count"], 1
        )

    def test_migration_is_append_only_and_reversible(self) -> None:
        conn = sqlite3.connect(":memory:")
        apply_schema(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(revenue_poc_discovery_snapshots)")}
        self.assertIn("source_event_category", columns)
        conn.execute(
            "INSERT INTO revenue_poc_discovery_snapshots "
            "(run_id,timestamp_utc,venue,market_id,status,metadata) VALUES (?,?,?,?,?,?)",
            ("run", "2026-08-06T00:00:00Z", "polymarket", "m", "REJECTED", "{}"),
        )
        conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM revenue_poc_discovery_snapshots").fetchone()[0]
        apply_schema(conn)
        after = conn.execute("SELECT COUNT(*) FROM revenue_poc_discovery_snapshots").fetchone()[0]
        self.assertEqual(before, after)
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE revenue_poc_discovery_snapshots SET reporting_class='sports'")
        downgrade_schema(conn)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='revenue_poc_discovery_snapshots'"
            ).fetchone()[0],
            0,
        )

    def test_script_runs_from_repo_root_home_and_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            state = temp_path / "state"
            signals = state / "signals"
            signals.mkdir(parents=True)
            (signals / "infer_pipeline_report.json").write_text("{}", encoding="utf-8")
            db = state / "runs.sqlite"
            sqlite3.connect(db).close()
            env_file = temp_path / "runtime.env"
            env_file.write_text(
                f"BGL_SIGNALS_DIR={signals}\nBGL_DB_PATH={db}\n",
                encoding="utf-8",
            )
            env = dict(os.environ, BGL_RUNTIME_ENV_FILE=str(env_file))
            command = [sys.executable, str(ROOT / "scripts" / "discovery_breakdown.py")]
            for cwd in (ROOT, Path.home(), Path("/tmp")):
                result = subprocess.run(
                    command,
                    cwd=cwd,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("DISCOVERY BREAKDOWN", result.stdout)


if __name__ == "__main__":
    unittest.main()
