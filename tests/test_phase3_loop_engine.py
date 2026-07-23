from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import live_runner
from loop_engine.config import LLMBudget, LoopEngineConfig
from loop_engine.opportunity import score_opportunity
from loop_engine.prompts import (
    build_forecast_prompts,
    classify_market,
    prompt_family_for_category,
)
from loop_engine.skeptic import should_request_skeptic
from scripts import morning_status


def _create_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO kv(key,value) VALUES ('infer_cursor','0');
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
    return conn


def _market(slug: str, question: str, *, liquidity: float = 100_000) -> dict:
    return {
        "id": slug,
        "slug": slug,
        "question": question,
        "active": True,
        "closed": False,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.40", "0.60"]',
        "bestBid": 0.39,
        "bestAsk": 0.41,
        "lastTradePrice": 0.40,
        "volume": 2_000_000,
        "liquidity": liquidity,
        "endDate": "2026-10-01T00:00:00Z",
    }


class FakeAdapter:
    def __init__(self, markets: dict[str, dict]) -> None:
        self.markets = markets

    def get_market(self, slug: str) -> dict:
        return self.markets[slug]


class Phase3LoopEngineTests(unittest.TestCase):
    def test_opportunity_score_rewards_quality_and_rejects_weak_market(self) -> None:
        temporal = {
            "market_end_date": "2026-10-01T00:00:00Z",
            "time_remaining_hours": 1000.0,
        }
        strong = score_opportunity(
            _market("strong", "Will the Fed cut rates?"),
            category="macro/fed",
            p_yes_market=0.40,
            spread=0.01,
            temporal_context=temporal,
            min_score_for_llm=55.0,
        )
        weak = score_opportunity(
            _market("weak", "Will aliens appear?", liquidity=100.0),
            category="novelty/other",
            p_yes_market=0.02,
            spread=0.08,
            temporal_context={"market_end_date": None, "time_remaining_hours": None},
            min_score_for_llm=55.0,
            quality_reject_reason="low_liquidity",
        )
        self.assertGreater(strong.opportunity_score, weak.opportunity_score)
        self.assertTrue(strong.eligible_for_llm)
        self.assertEqual(weak.skip_reason, "weak_market_quality")
        self.assertIn("liquidity_quality", strong.scoring_components)

    def test_llm_budget_enforces_cycle_skeptic_and_daily_caps(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "usage.json"
            config = LoopEngineConfig(
                max_llm_calls_per_cycle=1,
                max_skeptic_calls_per_cycle=1,
                max_daily_llm_calls=2,
            )
            budget = LLMBudget(
                config,
                path,
                now=datetime(2026, 7, 23, tzinfo=timezone.utc),
            )
            self.assertTrue(budget.reserve_primary())
            self.assertFalse(budget.reserve_primary())
            self.assertTrue(budget.reserve_skeptic())
            self.assertEqual(budget.skeptic_status(), "daily_cap_reached")
            payload = json.loads(path.read_text())
            self.assertEqual(payload["calls_used"], 2)

    def test_prompt_routing_covers_required_families_and_temporal_fields(self) -> None:
        cases = {
            "Will the FOMC cut rates?": "macro/fed",
            "Will CPI exceed 3%?": "macro/econ",
            "Will the nominee win the election?": "politics",
            "Will Bitcoin exceed $100k?": "crypto",
            "Will SCOTUS accept the case?": "legal",
            "Will China invade Taiwan?": "geopolitics",
            "Will the NBA team win?": "sports",
            "Will a new album arrive first?": "novelty/other",
        }
        for question, expected in cases.items():
            self.assertEqual(classify_market(question), expected)
            self.assertEqual(prompt_family_for_category(expected), expected)

        temporal = {
            "current_utc": "2026-07-23T12:00:00Z",
            "current_date": "2026-07-23",
            "market_end_date": "2026-10-01T00:00:00Z",
            "market_resolution_date": "2026-10-01T00:00:00Z",
            "time_remaining": "69 days",
            "event_status": "ONGOING",
            "temporal_source": "endDate",
        }
        _, prompt = build_forecast_prompts(
            question="Will the FOMC cut rates?",
            venue="polymarket",
            p_yes_market=0.4,
            market_snapshot={"updatedAt": "2026-07-23T11:00:00Z"},
            temporal_context=temporal,
            category="macro/fed",
        )
        self.assertIn("2026-07-23T12:00:00Z", prompt)
        self.assertIn("2026-10-01T00:00:00Z", prompt)
        self.assertIn("Temporal self-check", prompt)
        self.assertIn("Common failures", prompt)

    def test_skeptic_trigger_logic_targets_threshold_and_risky_context(self) -> None:
        base = {
            "event_status": "ONGOING",
            "market_end_date": "2026-10-01T00:00:00Z",
        }
        triggered, reason = should_request_skeptic(
            edge_abs=0.05,
            confidence=0.7,
            category="macro/econ",
            temporal_context=base,
            candidate_threshold=0.04,
            near_threshold_ratio=0.75,
            high_confidence=0.85,
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, "candidate_edge")
        triggered, reason = should_request_skeptic(
            edge_abs=0.01,
            confidence=0.9,
            category="legal",
            temporal_context=base,
            candidate_threshold=0.04,
            near_threshold_ratio=0.75,
            high_confidence=0.85,
        )
        self.assertTrue(triggered)
        self.assertEqual(reason, "high_confidence_risky_category")

    def test_budget_skip_brain_report_and_nonpaper_db_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            watchlist_path = root / "watchlist.json"
            signals_dir = root / "signals"
            db_path = root / "runs.sqlite"
            slugs = ["fed-one", "fed-two"]
            watchlist_path.write_text(
                json.dumps([{"market_id": slug} for slug in slugs]),
                encoding="utf-8",
            )
            conn = _create_db(db_path)
            before = db_path.read_bytes()
            markets = {
                slug: _market(slug, f"Will the FOMC cut rates for {slug}?")
                for slug in slugs
            }
            env = {
                "BGL_INFER_BATCH": "2",
                "BGL_INFER_COOLDOWN": "0",
                "BGL_INFER_USE_LLM": "1",
                "BGL_MAX_LLM_CALLS_PER_CYCLE": "1",
                "BGL_MAX_SKEPTIC_CALLS_PER_CYCLE": "1",
                "BGL_MAX_DAILY_LLM_CALLS": "20",
                "BGL_MIN_OPPORTUNITY_SCORE_FOR_LLM": "0",
                "BGL_MIN_EDGE_ABS": "0.04",
                "BGL_MIN_EDGE_VS_MARKET": "0.04",
                "BGL_MAX_DISAGREEMENT": "0.60",
            }
            with (
                mock.patch.object(live_runner, "WATCHLIST_PATH", watchlist_path),
                mock.patch.object(live_runner, "SIGNALS_DIR", signals_dir),
                mock.patch.object(
                    live_runner, "get_adapter", return_value=FakeAdapter(markets)
                ),
                mock.patch.object(live_runner, "openai_enabled", return_value=True),
                mock.patch.object(
                    live_runner,
                    "forecast_yes_probability",
                    return_value=(0.41, 0.75, "Small, temporally valid difference."),
                ) as forecast,
                mock.patch.dict(os.environ, env, clear=False),
            ):
                candidate, report = live_runner._infer_one(
                    conn=conn,
                    venue="polymarket",
                    paper_size=100.0,
                    persist_state=False,
                )

            conn.close()
            self.assertIsNone(candidate)
            self.assertEqual(forecast.call_count, 1)
            self.assertEqual(report["summary"]["budget_skipped"], 1)
            reasons = {row["reason"] for row in report["markets"]}
            self.assertIn("budget_skipped", reasons)
            self.assertEqual(db_path.read_bytes(), before)

            brain_path = signals_dir / "swarm_brain_report.json"
            brain = json.loads(brain_path.read_text())
            self.assertEqual(brain["watchlist_total"], 2)
            self.assertEqual(brain["sampled_markets"], 2)
            self.assertEqual(brain["llm_calls_used"], 1)
            self.assertEqual(brain["skeptic_calls_used"], 0)
            self.assertEqual(len(brain["opportunity_rankings"]), 2)
            self.assertEqual(len(brain["market_records"]), 2)
            required = {
                "market_id",
                "question",
                "category",
                "opportunity_score",
                "opportunity_grade",
                "final_decision",
                "final_reason",
                "p_yes_market",
                "p_yes_model",
                "edge",
                "llm_used",
                "skeptic_used",
                "temporal_status",
                "budget_status",
                "scoring_components",
                "short_rationale_summary",
            }
            self.assertTrue(required.issubset(brain["market_records"][0]))

            with mock.patch.object(morning_status, "BRAIN_PATH", brain_path):
                from contextlib import redirect_stdout
                import io

                output = io.StringIO()
                with redirect_stdout(output):
                    morning_status.check_loop_engine()
            rendered = output.getvalue()
            self.assertIn("LOOP ENGINE", rendered)
            self.assertIn("calls  llm=1", rendered)
            self.assertIn("top opportunities", rendered)


if __name__ == "__main__":
    unittest.main()
