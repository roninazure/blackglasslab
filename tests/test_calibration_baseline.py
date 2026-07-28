from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import calibration_baseline as calibration
from scripts import resolve_paper_trades as resolver


def _create_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE paper_trades (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id TEXT NOT NULL,
          ts_utc TEXT NOT NULL,
          market_id TEXT NOT NULL,
          question TEXT NOT NULL,
          venue TEXT NOT NULL,
          side TEXT NOT NULL,
          consensus_p_yes REAL NOT NULL,
          disagreement REAL NOT NULL,
          size_usd REAL NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL,
          resolved_outcome TEXT,
          p_yes REAL NOT NULL,
          edge REAL NOT NULL,
          brier REAL,
          notes TEXT NOT NULL
        )
        """
    )

    def insert(
        trade_id: int,
        *,
        status: str,
        outcome: str | None,
        model_p: float,
        side: str,
        notes: str,
        stored_brier: float | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO paper_trades
            (id,run_id,ts_utc,market_id,question,venue,side,consensus_p_yes,
             disagreement,size_usd,reason,status,resolved_outcome,p_yes,edge,brier,notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                trade_id,
                f"run-{trade_id}",
                "2026-01-01T00:00:00+00:00",
                f"market-{trade_id}",
                f"Question {trade_id}?",
                "polymarket",
                side,
                model_p,
                0.1,
                100.0,
                "infer",
                status,
                outcome,
                model_p,
                0.2,
                stored_brier,
                notes,
            ),
        )

    base_yes = json.dumps(
        {
            "category": "test",
            "p_yes_market": 0.6,
            "edge_vs_market": 0.2,
            "llm": {"model": "fixture-model"},
        }
    )
    resolution_yes = json.dumps(
        {
            "resolution": {
                "resolved_at_utc": "2026-02-01T00:00:00+00:00",
                "profit_usd": 66.6667,
                "lookup_source": "slug",
            }
        }
    )
    insert(
        1,
        status="CLOSED",
        outcome="YES",
        model_p=0.8,
        side="YES",
        notes=base_yes + "\n" + resolution_yes,
        stored_brier=0.04,
    )

    base_no = json.dumps(
        {
            "category": "test",
            "p_yes_market": 0.4,
            "edge_vs_market": -0.2,
            "llm": {"model": "fixture-model"},
        }
    )
    resolution_no = json.dumps(
        {
            "resolution": {
                "resolved_at_utc": "2026-02-02T00:00:00+00:00",
                "profit_usd": 66.6667,
                "lookup_source": "snapshot_id",
            }
        }
    )
    insert(
        2,
        status="CLOSED",
        outcome="NO",
        model_p=0.2,
        side="NO",
        notes=base_no + "\n" + resolution_no,
        stored_brier=0.04,
    )
    insert(
        3,
        status="VOID",
        outcome="VOID",
        model_p=0.9,
        side="YES",
        notes="{}",
        stored_brier=None,
    )
    insert(
        4,
        status="OPEN",
        outcome=None,
        model_p=0.5,
        side="YES",
        notes="{}",
        stored_brier=None,
    )
    insert(
        5,
        status="CLOSED",
        outcome="YES",
        model_p=0.7,
        side="YES",
        notes="{}",
        stored_brier=0.09,
    )
    conn.commit()
    conn.close()


class CalibrationBaselineTests(unittest.TestCase):
    def test_metrics_exclusions_missing_fields_and_read_only_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "fixture.sqlite"
            output_dir = root / "reports"
            _create_database(db_path)
            before = hashlib.sha256(db_path.read_bytes()).hexdigest()

            report = calibration.analyze_database(db_path)
            calibration.write_reports(report, output_dir)

            after = hashlib.sha256(db_path.read_bytes()).hexdigest()
            self.assertEqual(after, before)
            self.assertEqual(report["valid_for"]["calibration_scoring_ids"], [1, 2, 5])
            self.assertAlmostEqual(report["overall"]["model_brier"], (0.04 + 0.04 + 0.09) / 3)
            self.assertAlmostEqual(report["overall"]["market_brier"], 0.16)
            self.assertAlmostEqual(report["overall"]["brier_skill_score_vs_market"], 0.75)

            exclusions = {row["id"]: row["exclusion_reason"] for row in report["excluded_trades"]}
            self.assertEqual(exclusions[3], "status_void")
            self.assertEqual(exclusions[4], "status_open_unresolved")

            missing = next(row for row in report["included_trades"] if row["id"] == 5)
            self.assertEqual(missing["model"], "unknown")
            self.assertEqual(missing["category"], "unknown")
            self.assertIsNone(missing["market_probability"])
            self.assertEqual(missing["profit_source"], "derived_resolver_even_money_fallback")

            self.assertTrue((output_dir / "phase2_calibration_baseline.json").exists())
            self.assertTrue((output_dir / "phase2_calibration_baseline.md").exists())
            self.assertTrue((output_dir / "phase2_calibration_trades.csv").exists())

    def test_brier_and_skill_math(self) -> None:
        self.assertAlmostEqual(calibration.brier_score(0.8, 1), 0.04)
        self.assertAlmostEqual(calibration.brier_score(0.4, 0), 0.16)
        self.assertAlmostEqual(calibration.brier_skill_score(0.04, 0.16), 0.75)
        self.assertIsNone(calibration.brier_skill_score(0.04, 0.0))

    def test_yes_no_and_losing_profit_math(self) -> None:
        cases = [
            ("YES", 100.0, "YES", 0.4, 150.0),
            ("NO", 100.0, "NO", 0.4, 66.6667),
            ("YES", 100.0, "NO", 0.4, -100.0),
            ("NO", 100.0, "YES", 0.4, -100.0),
        ]
        for side, size, outcome, market_p, expected in cases:
            analytics_profit = calibration.theoretical_profit_usd(
                side, size, outcome, market_p
            )
            resolver_profit = resolver.compute_profit_usd(
                side, size, outcome, market_p
            )
            self.assertAlmostEqual(analytics_profit, expected)
            self.assertEqual(analytics_profit, resolver_profit)


if __name__ == "__main__":
    unittest.main()
