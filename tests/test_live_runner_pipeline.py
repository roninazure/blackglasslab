from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import live_runner
from loop_engine.skeptic import SkepticReview


def _create_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
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
    return conn


def _market(slug: str, question: str) -> dict:
    return {
        "id": "123",
        "slug": slug,
        "question": question,
        "active": True,
        "closed": False,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.40", "0.60"]',
        "bestBid": 0.39,
        "bestAsk": 0.41,
        "lastTradePrice": 0.40,
        "volume": 500000,
        "liquidity": 50000,
        "endDate": "2030-01-01T00:00:00Z",
    }


class FakeAdapter:
    def get_market(self, slug: str) -> dict:
        if slug == "fetch-fails":
            raise RuntimeError("fixture fetch failure")
        questions = {
            "politics-cap": "Will the election result be certified?",
            "candidate": "Will Bitcoin exceed the target?",
        }
        return _market(slug, questions.get(slug, slug))


class PipelineTests(unittest.TestCase):
    def test_complete_funnel_records_skips_failures_and_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            watchlist_path = root / "watchlist.json"
            watchlist = ["already-open", "politics-cap", "fetch-fails", "candidate"]
            watchlist_path.write_text(
                json.dumps([{"market_id": slug} for slug in watchlist]),
                encoding="utf-8",
            )
            conn = _create_db(root / "runs.sqlite")
            conn.execute(
                """
                INSERT INTO paper_trades
                (run_id,ts_utc,market_id,question,venue,side,consensus_p_yes,
                 disagreement,size_usd,reason,status,p_yes,edge,notes)
                VALUES ('r','t','already-open','q','polymarket','YES',0.6,
                        0.1,100,'infer','OPEN',0.6,0.2,?)
                """,
                (json.dumps({"category": "politics"}),),
            )
            conn.commit()

            env = {
                "BGL_INFER_BATCH": "4",
                "BGL_INFER_COOLDOWN": "0",
                "BGL_INFER_USE_LLM": "1",
                "BGL_MAX_PER_CATEGORY": "1",
                "BGL_MIN_EDGE_ABS": "0.03",
                "BGL_MIN_EDGE_VS_MARKET": "0.03",
                "BGL_MAX_DISAGREEMENT": "0.60",
                "BGL_MARKET_UNIVERSE_POLICY_MODE": "off",
            }
            with (
                mock.patch.object(live_runner, "WATCHLIST_PATH", watchlist_path),
                mock.patch.object(live_runner, "SIGNALS_DIR", root / "signals"),
                mock.patch.object(live_runner, "get_adapter", return_value=FakeAdapter()),
                mock.patch.object(live_runner, "openai_enabled", return_value=True),
                mock.patch.object(
                    live_runner,
                    "forecast_yes_probability",
                    return_value=(0.70, 0.90, "fixture rationale"),
                ),
                mock.patch.object(
                    live_runner,
                    "review_forecast",
                    return_value=SkepticReview(
                        action="ALLOW",
                        reason="supported",
                        rationale="fixture critic",
                        temporal_valid=True,
                        stale_facts=False,
                        malformed_or_novelty=False,
                        edge_real=True,
                    ),
                ),
                mock.patch.dict(os.environ, env, clear=False),
            ):
                candidate, report = live_runner._infer_one(
                    conn=conn, venue="polymarket", paper_size=100.0
                )

            by_id = {row["market_id"]: row for row in report["markets"]}
            self.assertEqual(set(by_id), set(watchlist))
            self.assertNotIn("unclassified", {row["reason"] for row in report["markets"]})
            self.assertEqual(
                by_id["already-open"]["reason"], "existing_open_or_pending_position"
            )
            self.assertEqual(by_id["politics-cap"]["reason"], "category_cap_reached")
            self.assertEqual(by_id["fetch-fails"]["reason"], "fetch_failed")
            self.assertEqual(by_id["candidate"]["decision"], "CANDIDATE")
            self.assertEqual(candidate["market_id"], "candidate")

            summary = report["summary"]
            self.assertEqual(summary["watchlist_total"], len(watchlist))
            self.assertEqual(summary["finalized_markets"], len(watchlist))
            self.assertEqual(summary["blocked_existing_position"], 1)
            self.assertEqual(summary["skipped_category_cap"], 1)
            self.assertEqual(summary["fetch_failed"], 1)
            self.assertEqual(summary["candidates_generated"], 1)
            self.assertEqual(summary["diagnostics_written"], 3)
            conn.close()

    def test_paper_approval_gate_and_duplicate_behavior_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = _create_db(Path(td) / "runs.sqlite")
            candidate = {
                "run_id": "infer-test",
                "ts_utc": "2026-01-01T00:00:00+00:00",
                "market_id": "candidate",
                "question": "Question?",
                "venue": "polymarket",
                "side": "YES",
                "consensus_p_yes": 0.7,
                "disagreement": 0.1,
                "size_usd": 100.0,
                "reason": "infer",
                "p_yes": 0.7,
                "edge": 0.2,
                "notes": {},
            }
            with mock.patch.dict(os.environ, {"BGL_REQUIRE_APPROVAL": "1"}):
                self.assertEqual(
                    live_runner._insert_paper_trade(conn, candidate),
                    "queued_for_approval",
                )
            self.assertEqual(
                conn.execute("SELECT status FROM paper_trades").fetchone()[0], "PENDING"
            )
            self.assertEqual(
                live_runner._insert_paper_trade(conn, candidate), "skipped_duplicate"
            )
            conn.close()


if __name__ == "__main__":
    unittest.main()
