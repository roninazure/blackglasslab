from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import live_runner
from loop_engine.config import LoopEngineConfig
from loop_engine.opportunity import score_opportunity
from loop_engine.shadow import (
    brier_score,
    ensure_shadow_schema,
    hypothetical_profit,
    insert_shadow_forecast,
    resolve_shadow_forecast,
    shadow_summary,
)
from scripts import morning_status


def _paper_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE paper_trades (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id TEXT NOT NULL, ts_utc TEXT NOT NULL, market_id TEXT NOT NULL,
          question TEXT NOT NULL, venue TEXT NOT NULL, side TEXT NOT NULL,
          consensus_p_yes REAL NOT NULL, disagreement REAL NOT NULL,
          size_usd REAL NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL,
          resolved_outcome TEXT, p_yes REAL NOT NULL, edge REAL NOT NULL,
          brier REAL, notes TEXT NOT NULL
        );
        """
    )
    conn.commit()


def _forecast(**overrides: object) -> dict:
    base = {
        "run_id": "infer-fixture",
        "timestamp_utc": "2026-07-01T00:00:00+00:00",
        "venue": "polymarket",
        "market_id": "fixture-market",
        "slug": "fixture-market",
        "question": "Will the fixture resolve yes?",
        "category": "macro/econ",
        "market_probability": 0.40,
        "model_probability": 0.445,
        "side": "YES",
        "absolute_edge": 0.045,
        "opportunity_score": 80.0,
        "quality_score": 82.0,
        "grade": "B",
        "contract_validity": "valid",
        "opportunity_quality": "qualified",
        "model_edge": "meets_production_threshold",
        "production_decision": "candidate_pending_approval",
        "market_end_date": "2026-07-11T00:00:00+00:00",
        "temporal_validation": "valid",
        "llm_used": True,
    }
    base.update(overrides)
    return base


class FakeAdapter:
    def get_market(self, slug: str) -> dict:
        return {
            "id": "12345",
            "slug": slug,
            "question": "Will CPI exceed the target?",
            "active": True,
            "closed": False,
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.40", "0.60"]',
            "bestBid": 0.395,
            "bestAsk": 0.405,
            "lastTradePrice": 0.40,
            "volume": 2_000_000,
            "liquidity": 100_000,
            "endDate": "2026-08-15T00:00:00Z",
        }


class Phase33ShadowTests(unittest.TestCase):
    def test_schema_migration_backs_up_existing_database(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "runs.sqlite"
            conn = sqlite3.connect(db_path)
            _paper_schema(conn)
            conn.execute(
                """
                INSERT INTO paper_trades
                (run_id,ts_utc,market_id,question,venue,side,consensus_p_yes,
                 disagreement,size_usd,reason,status,p_yes,edge,notes)
                VALUES ('old','2026-01-01','old-market','q','polymarket','YES',
                        .6,.1,100,'infer','CLOSED',.6,.1,'{}')
                """
            )
            conn.commit()
            backup = ensure_shadow_schema(
                conn,
                db_path=db_path,
                backup_dir=root / "backups",
                now=datetime(2026, 7, 28, tzinfo=timezone.utc),
            )
            self.assertIsNotNone(backup)
            self.assertTrue(backup.exists())
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0], 1
            )
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("shadow_forecasts", tables)
            self.assertIn("shadow_threshold_results", tables)
            self.assertIsNone(
                ensure_shadow_schema(
                    conn, db_path=db_path, backup_dir=root / "backups"
                )
            )
            conn.close()

    def test_insert_is_idempotent_assigns_thresholds_and_keeps_entry_immutable(self) -> None:
        conn = sqlite3.connect(":memory:")
        ensure_shadow_schema(conn)
        first = insert_shadow_forecast(
            conn,
            _forecast(),
            thresholds=(0.02, 0.03, 0.04, 0.05),
            production_threshold=0.04,
        )
        duplicate = insert_shadow_forecast(
            conn,
            _forecast(model_probability=0.99),
            thresholds=(0.02, 0.03, 0.04, 0.05),
            production_threshold=0.04,
        )
        self.assertTrue(first.inserted)
        self.assertFalse(duplicate.inserted)
        self.assertEqual(first.forecast_id, duplicate.forecast_id)
        self.assertEqual(
            conn.execute("SELECT model_probability FROM shadow_forecasts").fetchone()[0],
            0.445,
        )
        rows = conn.execute(
            "SELECT bucket_label, qualifies FROM shadow_threshold_results ORDER BY id"
        ).fetchall()
        self.assertEqual(len(rows), 5)
        self.assertEqual(sum(int(row[1]) for row in rows), 4)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE shadow_forecasts SET model_probability=.9 WHERE id=?",
                (first.forecast_id,),
            )
        conn.close()

    def test_resolution_brier_pnl_roi_holding_period_and_summary(self) -> None:
        conn = sqlite3.connect(":memory:")
        ensure_shadow_schema(conn)
        inserted = insert_shadow_forecast(
            conn,
            _forecast(model_probability=0.8, absolute_edge=0.4),
            thresholds=(0.02, 0.03, 0.04, 0.05),
            production_threshold=0.04,
            hypothetical_stake_usd=100.0,
        )
        self.assertAlmostEqual(brier_score(0.8, "YES"), 0.04)
        self.assertAlmostEqual(
            hypothetical_profit(
                side="YES", stake_usd=100, outcome="YES", market_probability=0.4
            ),
            150.0,
        )
        self.assertTrue(
            resolve_shadow_forecast(
                conn,
                inserted.forecast_id,
                "YES",
                resolved_at_utc="2026-07-11T00:00:00+00:00",
            )
        )
        self.assertFalse(
            resolve_shadow_forecast(
                conn,
                inserted.forecast_id,
                "NO",
                resolved_at_utc="2026-07-12T00:00:00+00:00",
            )
        )
        row = conn.execute(
            """
            SELECT model_probability,eventual_outcome,brier_score,hypothetical_win,
                   hypothetical_pnl,roi,holding_period_days
            FROM shadow_forecasts
            """
        ).fetchone()
        self.assertEqual(row[0], 0.8)
        self.assertEqual(row[1], "YES")
        self.assertAlmostEqual(row[2], 0.04)
        self.assertEqual(row[3], 1)
        self.assertAlmostEqual(row[4], 150.0)
        self.assertAlmostEqual(row[5], 1.5)
        self.assertAlmostEqual(row[6], 10.0)
        summary = shadow_summary(
            conn, now=datetime(2026, 7, 11, tzinfo=timezone.utc)
        )
        self.assertEqual(summary["resolved_forecasts"], 1)
        self.assertEqual(summary["evaluations_per_cycle"], 1)
        self.assertEqual(summary["llm_calls_per_cycle"], 1)
        self.assertEqual(summary["best_performing_threshold"], ">=2%")
        self.assertTrue(summary["threshold_buckets"][0]["calibration_bands"])
        conn.close()

    def test_time_to_resolution_weight_prioritizes_shorter_horizon(self) -> None:
        market = {
            "question": "Will CPI exceed the target?",
            "liquidity": 100_000,
            "volume": 1_000_000,
        }
        short = score_opportunity(
            market,
            category="macro/econ",
            p_yes_market=0.4,
            spread=0.01,
            temporal_context={
                "market_end_date": "2026-08-01T00:00:00Z",
                "time_remaining_hours": 24 * 14,
            },
            min_score_for_llm=0,
            time_to_resolution_weight=2.0,
        )
        long = score_opportunity(
            market,
            category="macro/econ",
            p_yes_market=0.4,
            spread=0.01,
            temporal_context={
                "market_end_date": "2027-08-01T00:00:00Z",
                "time_remaining_hours": 24 * 400,
            },
            min_score_for_llm=0,
            time_to_resolution_weight=2.0,
        )
        self.assertGreater(short.opportunity_score, long.opportunity_score)
        self.assertGreater(long.opportunity_score, 0)

    def test_controlled_cycle_records_low_edge_shadow_without_paper_position(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "runs.sqlite"
            conn = sqlite3.connect(db_path)
            _paper_schema(conn)
            conn.execute(
                """
                INSERT INTO paper_trades
                (run_id,ts_utc,market_id,question,venue,side,consensus_p_yes,
                 disagreement,size_usd,reason,status,p_yes,edge,notes)
                VALUES ('history','2026-01-01','history','q','polymarket','YES',
                        .6,.1,100,'infer','CLOSED',.6,.1,'{}')
                """
            )
            conn.commit()
            before = conn.execute("SELECT * FROM paper_trades").fetchall()
            watchlist = root / "watchlist.json"
            watchlist.write_text(json.dumps([{"market_id": "low-edge"}]))
            baseline = SimpleNamespace(
                p_yes_market=0.4,
                p_yes_model=0.435,
                confidence=0.9,
                components={"fixture": True},
                reject_reason=None,
            )
            env = {
                "BGL_INFER_BATCH": "1",
                "BGL_EVALUATIONS_PER_CYCLE": "1",
                "BGL_INFER_USE_LLM": "0",
                "BGL_MIN_OPPORTUNITY_SCORE_FOR_LLM": "0",
                "BGL_MIN_EDGE_ABS": "0.04",
                "BGL_MIN_EDGE_VS_MARKET": "0.04",
                "BGL_MAX_DISAGREEMENT": "0.60",
                "BGL_MARKET_UNIVERSE_POLICY_MODE": "off",
                "BGL_SHADOW_LEDGER_ENABLED": "1",
                "BGL_SHADOW_THRESHOLD_BUCKETS": "0.02,0.03,0.04,0.05",
            }
            runtime_paths = SimpleNamespace(
                backup_dir=root / "backups",
            )

            with (
                mock.patch.object(live_runner, "WATCHLIST_PATH", watchlist),
                mock.patch.object(live_runner, "SIGNALS_DIR", root / "signals"),
                mock.patch.object(live_runner, "RUNTIME_PATHS", runtime_paths),
                mock.patch.object(
                    live_runner, "get_adapter", return_value=FakeAdapter()
                ),
                mock.patch.object(live_runner, "score_market", return_value=baseline),
                mock.patch.dict(os.environ, env, clear=False),
            ):
                candidate, report = live_runner._infer_one(
                    conn=conn,
                    venue="polymarket",
                    paper_size=100.0,
                    persist_state=True,
                    paper_mode=True,
                )
            self.assertIsNone(candidate)
            self.assertEqual(report["summary"]["shadow_inserted"], 1)
            self.assertEqual(
                conn.execute("SELECT * FROM paper_trades").fetchall(), before
            )
            qualified = dict(
                conn.execute(
                    "SELECT bucket_label, qualifies FROM shadow_threshold_results"
                ).fetchall()
            )
            self.assertEqual(qualified[">=2%"], 1)
            self.assertEqual(qualified[">=3%"], 1)
            self.assertEqual(qualified[">=4%"], 0)
            self.assertEqual(qualified["current production threshold"], 0)
            self.assertEqual(
                conn.execute(
                    "SELECT production_decision,rejection_reason FROM shadow_forecasts"
                ).fetchone(),
                ("rejected", "min_edge_abs"),
            )

            with mock.patch.object(morning_status, "DB_PATH", db_path):
                output = io.StringIO()
                with redirect_stdout(output):
                    morning_status.check_shadow_forecasts()
            rendered = output.getvalue()
            self.assertIn("forecasts today=1 total=1 resolved=0", rendered)
            self.assertIn(">=2%", rendered)
            self.assertIn("production decision", rendered)
            conn.close()

    def test_conservative_config_defaults_keep_shadow_on_and_production_at_four_percent(self) -> None:
        keys = (
            "BGL_ACTIVE_UNIVERSE_SIZE",
            "BGL_UNIVERSE_TARGET_SIZE",
            "BGL_EVALUATIONS_PER_CYCLE",
            "BGL_MAX_LLM_CALLS_PER_CYCLE",
            "BGL_SHADOW_THRESHOLD_BUCKETS",
            "BGL_TIME_TO_RESOLUTION_WEIGHT",
            "BGL_SHADOW_LEDGER_ENABLED",
            "BGL_MIN_EDGE_ABS",
            "BGL_MIN_EDGE",
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in keys:
                os.environ.pop(key, None)
            config = LoopEngineConfig.from_env()
            self.assertEqual(config.active_universe_size, 75)
            self.assertEqual(config.evaluations_per_cycle, 15)
            self.assertEqual(config.max_llm_calls_per_cycle, 3)
            self.assertEqual(config.threshold_buckets, (0.02, 0.03, 0.04, 0.05))
            self.assertTrue(config.shadow_ledger_enabled)
            self.assertEqual(live_runner._filters()[0], 0.04)


if __name__ == "__main__":
    unittest.main()
