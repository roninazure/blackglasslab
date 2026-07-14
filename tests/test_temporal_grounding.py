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
from context.temporal import build_temporal_context, validate_temporal_rationale


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


def _market(
    slug: str,
    question: str,
    *,
    end_date: str | None,
    active: bool = True,
    closed: bool = False,
) -> dict:
    market = {
        "id": "123",
        "slug": slug,
        "question": question,
        "active": active,
        "closed": closed,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.40", "0.60"]',
        "bestBid": 0.39,
        "bestAsk": 0.41,
        "lastTradePrice": 0.40,
        "volume": 500000,
        "liquidity": 50000,
    }
    if end_date is not None:
        market["endDate"] = end_date
    return market


class FakeAdapter:
    def __init__(self, markets: dict[str, dict]):
        self.markets = markets

    def get_market(self, slug: str) -> dict:
        if slug not in self.markets:
            raise RuntimeError(f"missing market fixture: {slug}")
        value = self.markets[slug]
        if isinstance(value, Exception):
            raise value
        return value


class TemporalGroundingTests(unittest.TestCase):
    def test_temporal_context_reports_unknown_without_end_date(self) -> None:
        ctx = build_temporal_context(
            _market("slug", "Question?", end_date=None),
            question="Question?",
            slug="slug",
            now=datetime(2026, 7, 13, tzinfo=timezone.utc),
        )
        self.assertEqual(ctx["event_status"], "UNKNOWN")
        self.assertEqual(ctx["market_end_date"], None)
        self.assertEqual(ctx["time_remaining"], "unknown")

    def test_validate_temporal_rationale_rejects_stale_date_claim(self) -> None:
        temporal_context = {
            "current_utc": "2026-07-13T00:00:00Z",
            "current_date": "2026-07-13",
            "market_end_date": "2027-01-01T00:00:00Z",
            "market_resolution_date": "2027-01-01T00:00:00Z",
            "time_remaining_hours": 4000.0,
            "time_remaining": "166.7 days",
            "event_status": "UPCOMING",
            "temporal_source": "endDate",
            "requires_verified_temporal_context": False,
        }
        ok, reason, details = validate_temporal_rationale(
            "GTA VI is expected in 2025, so the market is stale.",
            temporal_context,
            now=datetime(2026, 7, 13, tzinfo=timezone.utc),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "stale_date_claim")
        self.assertEqual(details["temporal_signal"], "stale_date_claim")

    def test_validate_temporal_rationale_rejects_impossible_relative_time_claim(self) -> None:
        temporal_context = {
            "current_utc": "2026-07-13T00:00:00Z",
            "current_date": "2026-07-13",
            "market_end_date": "2025-01-01T00:00:00Z",
            "market_resolution_date": "2025-01-01T00:00:00Z",
            "time_remaining_hours": -1200.0,
            "time_remaining": "50.0 days overdue",
            "event_status": "RESOLVED",
            "temporal_source": "endDate",
            "requires_verified_temporal_context": False,
        }
        ok, reason, details = validate_temporal_rationale(
            "This will happen in two weeks.",
            temporal_context,
            now=datetime(2026, 7, 13, tzinfo=timezone.utc),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "impossible_relative_time_claim")
        self.assertEqual(details["temporal_signal"], "relative_future_claim")

    def test_validate_temporal_rationale_allows_valid_future_reasoning(self) -> None:
        temporal_context = {
            "current_utc": "2026-07-13T00:00:00Z",
            "current_date": "2026-07-13",
            "market_end_date": "2027-01-01T00:00:00Z",
            "market_resolution_date": "2027-01-01T00:00:00Z",
            "time_remaining_hours": 4000.0,
            "time_remaining": "166.7 days",
            "event_status": "UPCOMING",
            "temporal_source": "endDate",
            "requires_verified_temporal_context": False,
        }
        ok, reason, _ = validate_temporal_rationale(
            "The event is still future-facing and the market remains unresolved.",
            temporal_context,
            now=datetime(2026, 7, 13, tzinfo=timezone.utc),
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_unknown_resolution_dates_degrade_safely(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            watchlist_path = root / "watchlist.json"
            watchlist_path.write_text(
                json.dumps([{"market_id": "unknown-date-market"}]),
                encoding="utf-8",
            )
            conn = _create_db(root / "runs.sqlite")
            env = {
                "BGL_INFER_BATCH": "1",
                "BGL_INFER_COOLDOWN": "0",
                "BGL_INFER_USE_LLM": "1",
                "BGL_MAX_PER_CATEGORY": "3",
                "BGL_MIN_EDGE_ABS": "0.00",
                "BGL_MIN_EDGE_VS_MARKET": "0.00",
                "BGL_MAX_DISAGREEMENT": "0.99",
            }
            market = _market(
                "unknown-date-market",
                "Will Bitcoin exceed the target?",
                end_date=None,
            )
            with (
                mock.patch.object(live_runner, "WATCHLIST_PATH", watchlist_path),
                mock.patch.object(live_runner, "SIGNALS_DIR", root / "signals"),
                mock.patch.object(live_runner, "get_adapter", return_value=FakeAdapter({"unknown-date-market": market})),
                mock.patch.object(live_runner, "openai_enabled", return_value=True),
                mock.patch.object(
                    live_runner,
                    "forecast_yes_probability",
                    return_value=(0.70, 0.90, "No temporal contradiction here."),
                ),
                mock.patch.object(live_runner, "_kv_set", lambda *args, **kwargs: None),
                mock.patch.dict(os.environ, env, clear=False),
            ):
                candidate, report = live_runner._infer_one(conn=conn, venue="polymarket", paper_size=100.0)

            self.assertIsNotNone(candidate)
            self.assertEqual(report["markets"][0]["reason"], "candidate_generated")
            self.assertEqual(report["summary"]["temporal_inconsistency"], 0)
            self.assertEqual(conn.total_changes, 0)
            conn.close()

    def test_gta_vi_markets_without_verified_context_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            watchlist_path = root / "watchlist.json"
            slugs = [
                "will-bitcoin-hit-1m-before-gta-vi-872-424",
                "will-china-invades-taiwan-before-gta-vi-716-644",
                "new-rhianna-album-before-gta-vi-926",
                "new-playboi-carti-album-before-gta-vi-421",
            ]
            watchlist_path.write_text(json.dumps([{"market_id": slug} for slug in slugs]), encoding="utf-8")
            conn = _create_db(root / "runs.sqlite")
            markets = {
                slug: _market(slug, f"Will {slug} resolve before GTA VI?", end_date=None)
                for slug in slugs
            }
            llm = mock.Mock(return_value=(0.78, 0.92, "irrelevant because context is unsafe"))
            env = {
                "BGL_INFER_BATCH": "4",
                "BGL_INFER_COOLDOWN": "0",
                "BGL_INFER_USE_LLM": "1",
                "BGL_MAX_PER_CATEGORY": "3",
                "BGL_MIN_EDGE_ABS": "0.00",
                "BGL_MIN_EDGE_VS_MARKET": "0.00",
                "BGL_MAX_DISAGREEMENT": "0.99",
            }
            with (
                mock.patch.object(live_runner, "WATCHLIST_PATH", watchlist_path),
                mock.patch.object(live_runner, "SIGNALS_DIR", root / "signals"),
                mock.patch.object(live_runner, "get_adapter", return_value=FakeAdapter(markets)),
                mock.patch.object(live_runner, "openai_enabled", return_value=True),
                mock.patch.object(live_runner, "forecast_yes_probability", llm),
                mock.patch.object(live_runner, "_kv_set", lambda *args, **kwargs: None),
                mock.patch.dict(os.environ, env, clear=False),
            ):
                candidate, report = live_runner._infer_one(conn=conn, venue="polymarket", paper_size=100.0)

            self.assertIsNone(candidate)
            self.assertEqual(llm.call_count, 0)
            reasons = {row["reason"] for row in report["markets"]}
            self.assertEqual(reasons, {"temporal_inconsistency"})
            self.assertEqual(report["summary"]["temporal_inconsistency"], 4)
            self.assertEqual(conn.total_changes, 0)
            conn.close()

    def test_temporal_validation_path_does_not_write_database(self) -> None:
        temporal_context = {
            "current_utc": "2026-07-13T00:00:00Z",
            "current_date": "2026-07-13",
            "market_end_date": "2027-01-01T00:00:00Z",
            "market_resolution_date": "2027-01-01T00:00:00Z",
            "time_remaining_hours": 4000.0,
            "time_remaining": "166.7 days",
            "event_status": "UPCOMING",
            "temporal_source": "endDate",
            "requires_verified_temporal_context": False,
        }
        with tempfile.TemporaryDirectory() as td:
            conn = _create_db(Path(td) / "runs.sqlite")
            before = conn.total_changes
            ok, reason, details = validate_temporal_rationale(
                "The event remains in the future.",
                temporal_context,
                now=datetime(2026, 7, 13, tzinfo=timezone.utc),
            )
            self.assertTrue(ok)
            self.assertEqual(reason, "ok")
            self.assertGreaterEqual(before, 0)
            self.assertEqual(conn.total_changes, before)
            self.assertEqual(details["event_status"], "UPCOMING")
            conn.close()


if __name__ == "__main__":
    unittest.main()
