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
from market_universe.policy import (
    BANNED_JUNK,
    InstitutionalUniverseConfig,
    evaluate_market,
)
from scripts.manage_watchlist import (
    apply_rebuild,
    build_rebuild_report,
    discover_market_universe,
)


NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)


def _market(
    slug: str,
    question: str,
    *,
    liquidity: float = 100_000.0,
    volume: float = 5_000_000.0,
    bid: float = 0.39,
    ask: float = 0.41,
    end_date: str | None = "2026-09-15T00:00:00Z",
) -> dict:
    market = {
        "id": slug,
        "slug": slug,
        "question": question,
        "active": True,
        "closed": False,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.40", "0.60"]',
        "bestBid": bid,
        "bestAsk": ask,
        "lastTradePrice": 0.40,
        "volume": volume,
        "liquidity": liquidity,
    }
    if end_date is not None:
        market["endDate"] = end_date
    return market


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


class FakeAdapter:
    def __init__(self, markets: dict[str, dict]) -> None:
        self.markets = markets

    def get_market(self, slug: str) -> dict:
        return self.markets[slug]


class MarketUniversePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = InstitutionalUniverseConfig()

    def test_gta_vi_markets_are_banned_by_default(self) -> None:
        result = evaluate_market(
            _market(
                "bitcoin-before-gta-vi",
                "Will Bitcoin reach $1 million before GTA VI releases?",
            ),
            config=self.config,
            now=NOW,
        )
        self.assertFalse(result.policy_allowed)
        self.assertEqual(result.classification, BANNED_JUNK)
        self.assertEqual(result.policy_reason, "banned_market_class")
        self.assertEqual(result.banned_class, "product_release_comparison")
        self.assertEqual(result.institutional_quality_score, 0.0)

    def test_album_and_celebrity_markets_are_banned_by_default(self) -> None:
        for slug, question in (
            ("new-rihanna-album", "Will Rihanna release a new album this year?"),
            ("celebrity-event", "Will a celebrity attend the awards ceremony?"),
        ):
            with self.subTest(slug=slug):
                result = evaluate_market(
                    _market(slug, question), config=self.config, now=NOW
                )
                self.assertFalse(result.policy_allowed)
                self.assertEqual(result.banned_class, "entertainment_celebrity")

    def test_malformed_market_is_rejected(self) -> None:
        result = evaluate_market(
            _market("bad-fed-question", "Will will the Fed cut rates?"),
            config=self.config,
            now=NOW,
        )
        self.assertFalse(result.policy_allowed)
        self.assertEqual(result.policy_reason, "malformed_market")
        self.assertEqual(result.banned_class, "malformed_market")

    def test_macro_fed_and_geopolitics_pass_quality_checks(self) -> None:
        questions = (
            ("fed-cut", "Will the Federal Reserve cut rates in September?"),
            ("ceasefire", "Will a ceasefire agreement take effect by September?"),
        )
        for slug, question in questions:
            with self.subTest(slug=slug):
                result = evaluate_market(
                    _market(slug, question), config=self.config, now=NOW
                )
                self.assertTrue(result.policy_allowed, result.as_dict())
                self.assertGreaterEqual(
                    result.institutional_quality_score,
                    self.config.min_quality_score,
                )

    def test_low_liquidity_and_high_spread_fail_quality(self) -> None:
        cases = (
            (
                _market(
                    "thin-fed",
                    "Will the Federal Reserve cut rates in September?",
                    liquidity=100.0,
                ),
                "low_liquidity",
            ),
            (
                _market(
                    "wide-fed",
                    "Will the Federal Reserve cut rates in September?",
                    bid=0.30,
                    ask=0.40,
                ),
                "wide_spread",
            ),
        )
        for market, reason_code in cases:
            with self.subTest(reason_code=reason_code):
                result = evaluate_market(
                    market, config=self.config, now=NOW
                )
                self.assertFalse(result.policy_allowed)
                self.assertEqual(
                    result.policy_reason, "low_institutional_quality"
                )
                self.assertIn(reason_code, result.reason_codes)

    def test_smaller_clean_watchlist_is_allowed_without_junk_fill(self) -> None:
        config = InstitutionalUniverseConfig(
            target_size=5,
            scan_pages=2,
            max_per_category=3,
        )
        pages = {
            0: [
                _market(
                    "fed-cut",
                    "Will the Federal Reserve cut rates in September?",
                ),
                _market(
                    "new-album",
                    "Will Rihanna release a new album this year?",
                ),
            ],
            100: [],
        }
        discovery = discover_market_universe(
            config=config,
            page_fetcher=lambda offset: pages[offset],
            now=NOW,
        )
        self.assertEqual(
            [row["market_id"] for row in discovery["selected"]],
            ["fed-cut"],
        )
        self.assertTrue(
            discovery["selection_summary"]["insufficient_clean_markets"]
        )
        self.assertEqual(
            discovery["selection_summary"]["insufficient_reason"],
            "insufficient_clean_markets",
        )

    def test_dry_run_report_does_not_modify_watchlist(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            watchlist = Path(td) / "watchlist.json"
            watchlist.write_text(
                json.dumps([{"market_id": "legacy"}]) + "\n",
                encoding="utf-8",
            )
            before = watchlist.read_bytes()
            audit = {
                "watchlist_size": 1,
                "summary": {
                    "classification_counts": {BANNED_JUNK: 1}
                },
                "markets": [{"market_id": "legacy"}],
            }
            discovery = {
                "selected": [
                    evaluate_market(
                        _market(
                            "fed-cut",
                            "Will the Federal Reserve cut rates in September?",
                        ),
                        config=self.config,
                        now=NOW,
                    ).as_dict()
                ],
                "scan": {
                    "pages_requested": 1,
                    "pages_fetched": 1,
                    "markets_scanned": 1,
                    "eligible_markets": 1,
                    "selected_markets": 1,
                },
                "selection_summary": {
                    "category_counts": {"macro/fed": 1},
                    "horizon_counts": {"46-120d": 1},
                    "classification_counts": {"INSTITUTIONAL_CORE": 1},
                    "rejection_reasons": {},
                    "balance_rejections": {},
                    "insufficient_clean_markets": True,
                    "insufficient_reason": "insufficient_clean_markets",
                },
            }
            report = build_rebuild_report(
                audit=audit,
                discovery=discovery,
                config=self.config,
                now=NOW,
            )
            self.assertTrue(report["dry_run"])
            self.assertTrue(report["materially_cleaner"])
            self.assertEqual(watchlist.read_bytes(), before)

    def test_apply_rebuild_backs_up_old_watchlist(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            watchlist = root / "watchlist.json"
            archive = root / "archive"
            old = json.dumps([{"market_id": "old-junk"}]) + "\n"
            watchlist.write_text(old, encoding="utf-8")
            selected = [
                {
                    "market_id": "fed-cut",
                    "classification": "INSTITUTIONAL_CORE",
                }
            ]
            backup = apply_rebuild(
                selected,
                watchlist_path=watchlist,
                archive_dir=archive,
                now=NOW,
            )
            self.assertEqual(backup.read_text(encoding="utf-8"), old)
            self.assertEqual(
                json.loads(watchlist.read_text()),
                [{"market_id": "fed-cut"}],
            )

    def test_loop_engine_bans_market_and_preserves_database(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "runs.sqlite"
            conn = _create_db(db_path)
            watchlist_path = root / "watchlist.json"
            watchlist_path.write_text(
                json.dumps([{"market_id": "bitcoin-before-gta-vi"}]),
                encoding="utf-8",
            )
            market = _market(
                "bitcoin-before-gta-vi",
                "Will Bitcoin reach $1 million before GTA VI releases?",
            )
            before = db_path.read_bytes()
            env = {
                "BGL_INFER_BATCH": "1",
                "BGL_INFER_COOLDOWN": "0",
                "BGL_INFER_USE_LLM": "1",
                "BGL_MARKET_UNIVERSE_POLICY_MODE": "institutional_v1",
            }
            forecast = mock.Mock()
            with (
                mock.patch.object(live_runner, "WATCHLIST_PATH", watchlist_path),
                mock.patch.object(live_runner, "SIGNALS_DIR", root / "signals"),
                mock.patch.object(
                    live_runner,
                    "get_adapter",
                    return_value=FakeAdapter(
                        {"bitcoin-before-gta-vi": market}
                    ),
                ),
                mock.patch.object(live_runner, "forecast_yes_probability", forecast),
                mock.patch.dict(os.environ, env, clear=False),
            ):
                candidate, report = live_runner._infer_one(
                    conn=conn,
                    venue="polymarket",
                    paper_size=100.0,
                    persist_state=False,
                    paper_mode=False,
                )
            conn.close()

            self.assertIsNone(candidate)
            self.assertEqual(forecast.call_count, 0)
            row = report["markets"][0]
            self.assertEqual(row["decision"], "REJECT")
            self.assertEqual(row["reason"], "banned_market_class")
            self.assertEqual(row["brain"]["opportunity_score"], 0.0)
            self.assertEqual(row["brain"]["opportunity_grade"], "F")
            self.assertFalse(row["brain"]["policy_allowed"])
            self.assertEqual(
                row["brain"]["banned_class"],
                "product_release_comparison",
            )
            brain = json.loads(
                (root / "signals" / "swarm_brain_report.json").read_text()
            )
            self.assertEqual(
                brain["market_universe_policy_mode"], "institutional_v1"
            )
            required = {
                "policy_allowed",
                "policy_reason",
                "institutional_quality_score",
                "banned_class",
            }
            self.assertTrue(required.issubset(brain["market_records"][0]))
            self.assertEqual(db_path.read_bytes(), before)

    def test_nonpaper_connection_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "runs.sqlite"
            writable = _create_db(db_path)
            writable.close()
            before = db_path.read_bytes()

            conn = live_runner._connect_db(
                str(db_path),
                read_only=True,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT value FROM kv WHERE key='infer_cursor'"
                ).fetchone()[0],
                "0",
            )
            with self.assertRaises(sqlite3.OperationalError):
                live_runner._kv_set(conn, "infer_cursor", "1")
            conn.close()
            self.assertEqual(db_path.read_bytes(), before)

    def test_approval_gate_and_runtime_defaults_remain_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = _create_db(Path(td) / "runs.sqlite")
            candidate = {
                "run_id": "test",
                "ts_utc": NOW.isoformat(),
                "market_id": "fed-cut",
                "question": "Will the Federal Reserve cut rates?",
                "venue": "polymarket",
                "side": "YES",
                "consensus_p_yes": 0.6,
                "disagreement": 0.1,
                "size_usd": 10.0,
                "reason": "infer",
                "p_yes": 0.6,
                "edge": 0.1,
                "notes": {},
            }
            with mock.patch.dict(
                os.environ, {"BGL_REQUIRE_APPROVAL": "1"}, clear=False
            ):
                result = live_runner._insert_paper_trade(conn, candidate)
            self.assertEqual(result, "queued_for_approval")
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM paper_trades"
                ).fetchone()[0],
                "PENDING",
            )
            conn.close()

        root = Path(__file__).resolve().parent.parent
        run_live = (root / "scripts" / "run_live.sh").read_text()
        wrapper = root / "bin" / "swarm-edge"
        self.assertIn('BGL_REQUIRE_APPROVAL="${BGL_REQUIRE_APPROVAL:-1}"', run_live)
        self.assertIn(
            'BGL_MARKET_UNIVERSE_POLICY_MODE="${BGL_MARKET_UNIVERSE_POLICY_MODE:-institutional_v1}"',
            run_live,
        )
        self.assertTrue(wrapper.exists())
        self.assertIn("run_live.sh", wrapper.read_text())


if __name__ == "__main__":
    unittest.main()
