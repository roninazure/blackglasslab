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
    POLICY_BANNED,
    POLICY_CORE,
    POLICY_RESEARCH,
    POLICY_WATCH,
    InstitutionalUniverseConfig,
    classify_institutional_category,
    evaluate_market,
)
from scripts.manage_watchlist import (
    apply_rebuild,
    build_expansion_report,
    build_rejection_analysis,
    discover_market_universe,
)


NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)


def _market(
    slug: str,
    question: str,
    *,
    liquidity: float = 150_000.0,
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


class InstitutionalCategoryTests(unittest.TestCase):
    def test_fed_and_rates_detection(self) -> None:
        self.assertEqual(
            classify_institutional_category(
                "Will the Federal Reserve cut rates in September?"
            ),
            "macro/fed",
        )
        self.assertEqual(
            classify_institutional_category(
                "Will the 10-year Treasury yield exceed 5%?"
            ),
            "rates",
        )

    def test_cpi_and_inflation_detection(self) -> None:
        self.assertEqual(
            classify_institutional_category(
                "Will CPI inflation exceed 3% in August?"
            ),
            "inflation/CPI",
        )

    def test_jobs_and_unemployment_detection(self) -> None:
        self.assertEqual(
            classify_institutional_category(
                "Will the unemployment rate exceed 5%?"
            ),
            "jobs/employment",
        )

    def test_gdp_and_growth_detection(self) -> None:
        self.assertEqual(
            classify_institutional_category(
                "Will US GDP growth exceed 2% in Q3?"
            ),
            "GDP/economic growth",
        )

    def test_oil_and_energy_detection(self) -> None:
        self.assertEqual(
            classify_institutional_category(
                "Will WTI crude oil exceed $100 by September?"
            ),
            "oil/gas",
        )

    def test_btc_and_eth_major_detection(self) -> None:
        for question in (
            "Will Bitcoin exceed $100,000 by December?",
            "Will Ethereum exceed $5,000 by December?",
        ):
            with self.subTest(question=question):
                self.assertEqual(
                    classify_institutional_category(question),
                    "crypto majors",
                )

    def test_geopolitics_detection(self) -> None:
        self.assertEqual(
            classify_institutional_category(
                "Will a ceasefire agreement take effect by September?"
            ),
            "geopolitics",
        )
        self.assertEqual(
            classify_institutional_category(
                "Will the US acquire part of Greenland in 2026?"
            ),
            "geopolitics",
        )

    def test_major_election_detection(self) -> None:
        self.assertEqual(
            classify_institutional_category(
                "Will the party win the 2026 general election?"
            ),
            "major elections",
        )

    def test_legal_and_regulatory_detection(self) -> None:
        self.assertEqual(
            classify_institutional_category(
                "Will the Supreme Court issue a ruling by October?"
            ),
            "legal/regulatory",
        )

    def test_central_bank_index_and_corporate_detection(self) -> None:
        cases = {
            "Will the ECB cut rates in September?": "central banks",
            "Will the S&P 500 exceed 7,000?": "major market indices",
            "Will the merger receive regulatory approval?": (
                "corporate/regulatory events"
            ),
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(
                    classify_institutional_category(question),
                    expected,
                )

    def test_event_metadata_expands_serious_detection(self) -> None:
        market = _market("macro-contract", "Will the value exceed 3%?")
        market["_discovery_event"] = {
            "title": "US CPI inflation for August 2026",
            "category": "Economy",
        }
        self.assertEqual(
            classify_institutional_category(market["question"], market),
            "inflation/CPI",
        )


class MarketUniversePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = InstitutionalUniverseConfig()

    def test_gta_novelty_and_entertainment_remain_banned(self) -> None:
        cases = (
            (
                "bitcoin-before-gta-vi",
                "Will Bitcoin reach $1 million before GTA VI releases?",
                "product_release_comparison",
            ),
            (
                "alien-disclosure",
                "Will aliens exist before December?",
                "novelty_meme",
            ),
            (
                "new-rihanna-album",
                "Will Rihanna release a new album this year?",
                "entertainment_celebrity",
            ),
        )
        for slug, question, banned_class in cases:
            with self.subTest(slug=slug):
                result = evaluate_market(
                    _market(slug, question),
                    config=self.config,
                    now=NOW,
                )
                self.assertEqual(result.policy_tier, POLICY_BANNED)
                self.assertEqual(result.banned_class, banned_class)
                self.assertFalse(result.policy_allowed)

    def test_local_primary_remains_banned_unless_enabled(self) -> None:
        market = _market(
            "candidate-for-az-01",
            "Will Jane Doe be the Republican nominee for AZ-01?",
        )
        banned = evaluate_market(market, config=self.config, now=NOW)
        self.assertEqual(banned.banned_class, "thin_local_primary")

        allowed_config = InstitutionalUniverseConfig(
            allow_thin_primaries=True
        )
        reviewed = evaluate_market(
            market,
            config=allowed_config,
            now=NOW,
        )
        self.assertNotEqual(reviewed.banned_class, "thin_local_primary")

    def test_malformed_market_is_rejected(self) -> None:
        result = evaluate_market(
            _market("bad-fed-question", "Will will the Fed cut rates?"),
            config=self.config,
            now=NOW,
        )
        self.assertEqual(result.policy_tier, POLICY_BANNED)
        self.assertEqual(result.policy_reason, "malformed_market")

    def test_tiered_policy_classification(self) -> None:
        core = evaluate_market(
            _market(
                "fed-core",
                "Will the Federal Reserve cut rates in September?",
            ),
            config=self.config,
            now=NOW,
        )
        research = evaluate_market(
            _market(
                "fed-research",
                "Will the Federal Reserve cut rates in October?",
                liquidity=20_000,
                volume=200_000,
                bid=0.38,
                ask=0.405,
            ),
            config=self.config,
            now=NOW,
        )
        watch = evaluate_market(
            _market(
                "fed-watch",
                "Will the Federal Reserve cut rates in November?",
                liquidity=1_000,
                volume=10_000,
            ),
            config=self.config,
            now=NOW,
        )
        self.assertEqual(core.policy_tier, POLICY_CORE)
        self.assertEqual(research.policy_tier, POLICY_RESEARCH)
        self.assertEqual(watch.policy_tier, POLICY_WATCH)
        self.assertTrue(core.policy_allowed)
        self.assertTrue(research.policy_allowed)
        self.assertFalse(watch.policy_allowed)

    def test_low_liquidity_and_high_spread_are_watch_only(self) -> None:
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
            with self.subTest(reason=reason_code):
                result = evaluate_market(
                    market,
                    config=self.config,
                    now=NOW,
                )
                self.assertEqual(result.policy_tier, POLICY_WATCH)
                self.assertIn(reason_code, result.reason_codes)

    def test_multi_sort_discovery_deduplicates_and_never_fills_with_banned(
        self,
    ) -> None:
        config = InstitutionalUniverseConfig(
            target_size=5,
            max_pages=1,
            sort_modes=("volume", "liquidity"),
            max_per_category=5,
        )
        fed = _market(
            "fed-cut",
            "Will the Federal Reserve cut rates in September?",
        )
        oil = _market(
            "wti-high",
            "Will WTI crude oil exceed $100 by September?",
        )
        junk = _market(
            "new-album",
            "Will Rihanna release a new album this year?",
        )
        pages = {
            "volume": [fed, junk],
            "liquidity": [fed, oil],
        }
        discovery = discover_market_universe(
            config=config,
            candidate_fetcher=lambda mode, offset, hint: (
                pages[mode] if offset == 0 else []
            ),
            now=NOW,
        )
        self.assertEqual(discovery["scan"]["raw_candidates"], 4)
        self.assertEqual(discovery["scan"]["unique_markets_scanned"], 3)
        self.assertEqual(
            {row["market_id"] for row in discovery["selected"]},
            {"fed-cut", "wti-high"},
        )
        self.assertTrue(
            discovery["selection_summary"]["insufficient_clean_markets"]
        )
        self.assertTrue(
            all(
                row["policy_tier"] in {POLICY_CORE, POLICY_RESEARCH}
                and not row["banned_class"]
                for row in discovery["selected"]
            )
        )

    def test_dry_run_report_does_not_modify_watchlist(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            watchlist = Path(td) / "watchlist.json"
            watchlist.write_text(
                json.dumps([{"market_id": "legacy"}]) + "\n",
                encoding="utf-8",
            )
            before = watchlist.read_bytes()
            good = evaluate_market(
                _market(
                    "fed-cut",
                    "Will the Federal Reserve cut rates in September?",
                ),
                config=self.config,
                now=NOW,
            ).as_dict()
            discovery = {
                "selected": [good],
                "evaluated": [good],
                "scan": {
                    "unique_markets_scanned": 1,
                    "eligible_core": 1,
                    "eligible_research": 0,
                    "watch_markets": 0,
                    "banned_markets": 0,
                    "eligible_markets": 1,
                    "selected_markets": 1,
                },
                "selection_summary": {
                    "category_counts": {"macro/fed": 1},
                    "horizon_counts": {"46-120d": 1},
                    "tier_counts": {POLICY_CORE: 1},
                    "classification_counts": {
                        "INSTITUTIONAL_CORE": 1
                    },
                    "balance_rejections": {},
                    "insufficient_clean_markets": True,
                    "insufficient_reason": "insufficient_clean_markets",
                },
            }
            audit = {
                "watchlist_size": 0,
                "markets": [],
            }
            analysis = build_rejection_analysis(
                discovery,
                config=self.config,
                now=NOW,
            )
            report = build_expansion_report(
                audit=audit,
                discovery=discovery,
                rejection_analysis=analysis,
                config=self.config,
                now=NOW,
            )
            self.assertTrue(report["dry_run"])
            self.assertTrue(report["materially_better"])
            self.assertEqual(watchlist.read_bytes(), before)

    def test_apply_rebuild_backs_up_old_watchlist(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            watchlist = root / "watchlist.json"
            archive = root / "archive"
            old = json.dumps([{"market_id": "old-junk"}]) + "\n"
            watchlist.write_text(old, encoding="utf-8")
            selected = [{"market_id": "fed-cut"}]
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

    def test_loop_brain_fields_and_database_preservation(self) -> None:
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
            forecast = mock.Mock()
            with (
                mock.patch.object(live_runner, "WATCHLIST_PATH", watchlist_path),
                mock.patch.object(live_runner, "SIGNALS_DIR", root / "signals"),
                mock.patch.object(
                    live_runner,
                    "UNIVERSE_REPORT_PATH",
                    root / "missing-expansion.json",
                ),
                mock.patch.object(
                    live_runner,
                    "get_adapter",
                    return_value=FakeAdapter(
                        {"bitcoin-before-gta-vi": market}
                    ),
                ),
                mock.patch.object(
                    live_runner,
                    "forecast_yes_probability",
                    forecast,
                ),
                mock.patch.dict(
                    os.environ,
                    {
                        "BGL_INFER_BATCH": "1",
                        "BGL_INFER_COOLDOWN": "0",
                        "BGL_INFER_USE_LLM": "1",
                        "BGL_MARKET_UNIVERSE_POLICY_MODE": (
                            "institutional_v2"
                        ),
                    },
                    clear=False,
                ),
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
            self.assertEqual(row["reason"], "banned_market_class")
            self.assertEqual(row["brain"]["policy_tier"], POLICY_BANNED)
            self.assertEqual(
                row["brain"]["institutional_category"],
                "crypto majors",
            )
            brain = json.loads(
                (root / "signals" / "swarm_brain_report.json").read_text()
            )
            self.assertIn("watchlist_tier_counts", brain)
            self.assertIn("rejection_distribution_summary", brain)
            self.assertEqual(
                brain["sampled_tier_counts"],
                {POLICY_BANNED: 1},
            )
            self.assertIn("market_universe", report)
            self.assertEqual(db_path.read_bytes(), before)

    def test_nonpaper_connection_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "runs.sqlite"
            writable = _create_db(db_path)
            writable.close()
            before = db_path.read_bytes()
            conn = live_runner._connect_db(str(db_path), read_only=True)
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
                os.environ,
                {"BGL_REQUIRE_APPROVAL": "1"},
                clear=False,
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
        self.assertIn(
            'BGL_REQUIRE_APPROVAL="${BGL_REQUIRE_APPROVAL:-1}"',
            run_live,
        )
        self.assertIn(
            'BGL_MARKET_UNIVERSE_POLICY_MODE="${BGL_MARKET_UNIVERSE_POLICY_MODE:-institutional_v2}"',
            run_live,
        )
        self.assertTrue(wrapper.exists())
        self.assertIn("run_live.sh", wrapper.read_text())


if __name__ == "__main__":
    unittest.main()
