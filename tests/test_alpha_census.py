from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from census.collector import (
    ALLOWED_HOSTS,
    PublicGetClient,
    _classify,
    _fee_metadata,
    negrisk_event_valid,
    read_collector_status,
    run_stream,
)
from census.durability import DurabilityTracker
from census.storage import SCHEMA, CensusStore
from census.stream import BookState, StreamStartupError, StreamStats, consume_market_stream


class AlphaCensusTests(unittest.TestCase):
    @staticmethod
    def observe(
        tracker: DurabilityTracker,
        key: str,
        now_ns: int,
        *,
        qualifying: bool,
        economic_executable: bool | None = True,
    ):
        return tracker.observe(
            key,
            now_ns,
            "2026-01-01T00:00:00Z",
            qualifying=qualifying,
            book_valid=qualifying,
            economic_executable=economic_executable,
            execution_validation_status=(
                "VALIDATED_EXECUTABLE"
                if economic_executable is True
                else "NOT_ECONOMICALLY_VALIDATED"
            ),
            durability_basis="test",
            bid=0.4 if qualifying else None,
            ask=0.6 if qualifying else None,
            depth_usd=1.0 if qualifying else None,
            gross_edge_usd=0.1 if qualifying else None,
            movement=None,
            adverse_selection=None,
        )

    def test_transport_is_public_get_only_and_allowlisted(self) -> None:
        self.assertEqual(ALLOWED_HOSTS, {"gamma-api.polymarket.com", "clob.polymarket.com"})
        with self.assertRaisesRegex(ValueError, "outside public allowlist"):
            PublicGetClient().get("https://evil.example/book")

    def test_sparse_store_persists_episode_not_quote_updates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "census.sqlite"; store = CensusStore(path)
            state = SimpleNamespace(key="k", started_at_utc="2026-01-01T00:00:00Z", end_ns=2_000_000_000, started_ns=0, lifetime_seconds=2.0, observed_executable_seconds=None, best_bid=.4, best_ask=.6, best_spread=.2, worst_spread=.3, best_depth_usd=10.0, worst_depth_usd=5.0, gross_edge_usd=None, fee_source="UNKNOWN", fee_rate=None, maker_rebate_rate=None, maker_rebate_economics="UNKNOWN", net_edge_usd=None, slippage_source="UNKNOWN", economic_executable=None, book_valid=True, execution_validation_status="NOT_ECONOMICALLY_VALIDATED", durability_basis="observable_two_sided_book", checkpoints={1.0: False, 5.0: None, 30.0: None, 60.0: None}, unknown_reason="stream_gap_timeout", quote_count=100, quote_movements=[.01], adverse_selection=[-.02], rejection_reason="fill_probability_not_measured")
            store.record_episode(state, engine="maker_spread_rebate", market_id="m", event_id="e", ended_at_utc="2026-01-01T00:00:02Z", metadata={}); store.commit()
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM opportunity_episodes").fetchone()[0], 1)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='observations'").fetchone()[0], 0)
            store.close()

    def test_engine_routing_does_not_make_binary_negrisk_structural(self) -> None:
        binary = {"negRisk": True, "markets": [{"id": "1", "clobTokenIds": ["y", "n"]}]}
        basket = {"negRisk": True, "markets": [{"id": str(i), "conditionId": str(i), "clobTokenIds": [f"y{i}", f"n{i}"]} for i in range(3)]}
        self.assertFalse(negrisk_event_valid(binary)); self.assertTrue(negrisk_event_valid(basket))
        self.assertEqual(_classify(binary["markets"][0], binary)[0], "unsupported")
        self.assertEqual(_classify(basket["markets"][0], basket)[0], "negrisk_structural")

    def test_continuous_executable_interval_survives_one_second(self) -> None:
        tracker = DurabilityTracker()
        self.observe(tracker, "x", 0, qualifying=True)
        self.observe(tracker, "x", 500_000_000, qualifying=True)
        transition = self.observe(tracker, "x", 1_100_000_000, qualifying=True)
        self.assertTrue(transition.active.checkpoints[1.0])

    def test_false_before_checkpoint_is_not_resurrected(self) -> None:
        tracker = DurabilityTracker()
        first = self.observe(tracker, "x", 0, qualifying=True).active
        lost = self.observe(tracker, "x", 500_000_000, qualifying=False)
        self.assertFalse(lost.closed[0].checkpoints[1.0])
        returned = self.observe(tracker, "x", 1_100_000_000, qualifying=True)
        self.assertNotEqual(first.key, returned.active.key)
        self.assertIsNone(returned.active.checkpoints[1.0])

    def test_reappearing_opportunity_persists_as_a_new_episode(self) -> None:
        tracker = DurabilityTracker()
        first = self.observe(tracker, "x", 0, qualifying=True)
        first_closed = self.observe(
            tracker, "x", 250_000_000, qualifying=False
        ).closed[0]
        second = self.observe(tracker, "x", 1_000_000_000, qualifying=True)
        second_closed = self.observe(
            tracker, "x", 1_250_000_000, qualifying=False
        ).closed[0]
        self.assertNotEqual(first.active.key, second.active.key)
        with tempfile.TemporaryDirectory() as td:
            store = CensusStore(Path(td) / "census.sqlite")
            for state in (first_closed, second_closed):
                store.record_episode(
                    state,
                    engine="negrisk_structural",
                    market_id="m",
                    event_id="e",
                    ended_at_utc="2026-01-01T00:00:02Z",
                    metadata={},
                )
            store.commit()
            self.assertEqual(
                store.conn.execute(
                    "SELECT COUNT(*) FROM opportunity_episodes"
                ).fetchone()[0],
                2,
            )
            store.close()

    def test_stream_gap_leaves_unobserved_checkpoint_unknown(self) -> None:
        tracker = DurabilityTracker()
        self.observe(tracker, "x", 0, qualifying=True)
        self.observe(tracker, "x", 500_000_000, qualifying=True)
        closed = tracker.expire(2_500_000_000)
        self.assertEqual(len(closed), 1)
        self.assertIsNone(closed[0].checkpoints[1.0])
        self.assertEqual(
            closed[0].checkpoint_reasons[1.0], "stream_gap_timeout"
        )

    def test_lifetime_excludes_stream_gap_timeout_padding(self) -> None:
        tracker = DurabilityTracker()
        self.observe(tracker, "x", 0, qualifying=True)
        self.observe(tracker, "x", 500_000_000, qualifying=True)
        closed = tracker.expire(2_500_000_000)[0]
        self.assertEqual(closed.lifetime_seconds, 0.5)
        self.assertEqual(closed.end_ns, 500_000_000)

    def test_unvalidated_engine_is_not_persisted_as_economically_executable(self) -> None:
        tracker = DurabilityTracker()
        state = self.observe(
            tracker,
            "maker",
            0,
            qualifying=True,
            economic_executable=None,
        ).active
        closed = self.observe(
            tracker,
            "maker",
            500_000_000,
            qualifying=False,
            economic_executable=None,
        ).closed[0]
        self.assertEqual(state.key, closed.key)
        with tempfile.TemporaryDirectory() as td:
            store = CensusStore(Path(td) / "census.sqlite")
            closed.rejection_reason = "fill_probability_not_measured"
            store.record_episode(
                closed,
                engine="maker_spread_rebate",
                market_id="m",
                event_id="e",
                ended_at_utc="2026-01-01T00:00:01Z",
                metadata={},
            )
            store.commit()
            row = store.conn.execute(
                "SELECT executable,theoretical,book_valid,execution_validation_status,"
                "durability_basis,net_executable_edge_usd,slippage_source,rejection_reason "
                "FROM opportunity_episodes"
            ).fetchone()
            self.assertEqual(row[:5], (0, 1, 1, "NOT_ECONOMICALLY_VALIDATED", "test"))
            self.assertEqual(row[5:], (None, "UNKNOWN", "fill_probability_not_measured"))
            closed.economic_executable = True
            with self.assertRaisesRegex(ValueError, "must remain UNKNOWN"):
                store.record_episode(
                    closed,
                    engine="maker_spread_rebate",
                    market_id="m",
                    event_id="e",
                    ended_at_utc="2026-01-01T00:00:01Z",
                    metadata={},
                )
            store.close()

    def test_episode_semantics_schema_upgrade_is_additive(self) -> None:
        legacy_schema = (
            SCHEMA.replace(
                "lifetime_seconds REAL NOT NULL, observed_executable_seconds REAL,",
                "lifetime_seconds REAL NOT NULL,",
            )
            .replace(", book_valid INTEGER", "")
            .replace(
                "\n  execution_validation_status TEXT NOT NULL DEFAULT 'LEGACY_UNKNOWN',",
                "",
            )
            .replace(
                "\n  durability_basis TEXT NOT NULL DEFAULT 'LEGACY_UNKNOWN',",
                "",
            )
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "legacy.sqlite"
            with sqlite3.connect(path) as conn:
                conn.executescript(legacy_schema)
            store = CensusStore(path)
            columns = {
                row[1]
                for row in store.conn.execute(
                    "PRAGMA table_info(opportunity_episodes)"
                )
            }
            self.assertTrue(
                {
                    "observed_executable_seconds",
                    "book_valid",
                    "execution_validation_status",
                    "durability_basis",
                }.issubset(columns)
            )
            store.close()

    def test_stream_stats_records_health(self) -> None:
        stats = StreamStats("2026-01-01T00:00:00Z"); stats.record("connection"); stats.record("disconnect", {"error": "x"}); stats.record("connection"); stats.record("reconnect"); stats.record("protocol_error"); stats.record("stale_stream")
        self.assertEqual((stats.connection_count, stats.reconnect_count, stats.disconnect_count, stats.protocol_error_count, stats.stale_stream_events), (2, 1, 1, 1, 1))

    def test_unknown_economics_are_explicit(self) -> None:
        self.assertEqual(_fee_metadata({})["fee_source"], "UNKNOWN")
        self.assertEqual(_fee_metadata({"feesEnabled": False})["fee_rate"], 0.0)

    def test_negrisk_structural_gross_is_not_overwritten_by_market_spread(self) -> None:
        control = {"captured_at_utc": "2026-01-01T00:00:00Z", "production_db_path": "/read-only-production.sqlite", "position_count": 0, "horizon_mix": {}, "realized_pnl_usd": 0.0, "unrealized_pnl_usd": 0.0, "deployed_capital_usd": 0.0, "opportunity_count": 0, "admission_count": 0, "status": "OK"}
        cycle = {"captured_at_utc": "2026-01-01T00:00:00Z", "cycle_id": "cycle", "cycle_timestamp_utc": "2026-01-01T00:00:00Z", "freshness_seconds": 1.0, "runner_pid": 1, "runner_state": "RUNNING", "cycle_state": "FRESH", "warning": None, "source": "test", "tolerance_seconds": 90.0}
        payload = [{"id": "event", "negRisk": True, "markets": [{"id": f"market-{index}", "conditionId": f"condition-{index}", "clobTokenIds": [f"yes-{index}", f"no-{index}"]} for index in range(3)]}]

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "census.sqlite"

            async def fake_consume(_assets, on_message, *, stop, stats, **_kwargs):
                base = time.monotonic_ns()
                for index in range(3):
                    stats.messages += 1
                    await on_message(
                        {"event_type": "book", "asset_id": f"yes-{index}", "bids": [{"price": "0.29", "size": "10"}], "asks": [{"price": "0.30", "size": "10"}]},
                        base + index * 100_000_000,
                    )
                stop.set()

            with patch("census.collector._read_revenue_control", return_value=control), patch("census.collector.authoritative_production_cycle", return_value=cycle), patch("census.collector.PublicGetClient.get", return_value=payload), patch("census.collector.consume_market_stream", new=fake_consume):
                asyncio.run(run_stream(db=db, pid=Path(td) / "census.pid", limit=1, duration_hours=None, log_path=Path(td) / "census.log"))
            with sqlite3.connect(db) as conn:
                row = conn.execute(
                    "SELECT gross_edge_usd,best_executable_depth_usd,best_spread,"
                    "executable,book_valid,execution_validation_status,durability_basis,"
                    "net_executable_edge_usd,slippage_source FROM opportunity_episodes "
                    "WHERE alpha_engine='negrisk_structural'"
                ).fetchone()
            self.assertAlmostEqual(row[0], 0.1)
            self.assertAlmostEqual(row[1], 3.0)
            self.assertIsNone(row[2])
            self.assertEqual(row[3:7], (1, 1, "VALIDATED_EXECUTABLE", "negrisk_structural_executable_basket"))
            self.assertEqual(row[7:], (None, "UNKNOWN"))

    def test_collector_heartbeat_advances_while_running(self) -> None:
        control = {"captured_at_utc": "2026-01-01T00:00:00Z", "production_db_path": "/read-only-production.sqlite", "position_count": 0, "horizon_mix": {}, "realized_pnl_usd": 0.0, "unrealized_pnl_usd": 0.0, "deployed_capital_usd": 0.0, "opportunity_count": 0, "admission_count": 0, "status": "OK"}
        cycle = {"captured_at_utc": "2026-01-01T00:00:00Z", "cycle_id": "cycle", "cycle_timestamp_utc": "2026-01-01T00:00:00Z", "freshness_seconds": 1.0, "runner_pid": 1, "runner_state": "RUNNING", "cycle_state": "FRESH", "warning": None, "source": "test", "tolerance_seconds": 90.0}
        payload = [{"id": "event", "markets": [{"id": "market", "clobTokenIds": ["asset", "other"]}]}]
        observed_updates = []

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "census.sqlite"

            async def fake_consume(_assets, on_message, *, stop, stats, **_kwargs):
                stats.messages = 1
                await on_message(
                    {"event_type": "book", "asset_id": "asset", "bids": [{"price": "0.4", "size": "10"}], "asks": [{"price": "0.6", "size": "10"}]},
                    time.monotonic_ns(),
                )
                observed_updates.append(read_collector_status(db)["updated_at_utc"])
                await asyncio.sleep(0.04)
                observed_updates.append(read_collector_status(db)["updated_at_utc"])
                stop.set()

            with patch("census.collector._read_revenue_control", return_value=control), patch("census.collector.authoritative_production_cycle", return_value=cycle), patch("census.collector.PublicGetClient.get", return_value=payload), patch("census.collector.consume_market_stream", new=fake_consume), patch("census.collector.COLLECTOR_HEARTBEAT_INTERVAL", 0.01):
                asyncio.run(run_stream(db=db, pid=Path(td) / "census.pid", limit=1, duration_hours=None, log_path=Path(td) / "census.log"))

        self.assertGreater(observed_updates[1], observed_updates[0])

    def test_stream_book_updates_are_incremental(self) -> None:
        books = BookState(); self.assertEqual(books.apply({"event_type": "book", "asset_id": "a", "bids": [{"price": "0.4", "size": "10"}], "asks": [{"price": "0.6", "size": "10"}]}), {"a"})
        books.apply({"event_type": "price_change", "price_changes": [{"asset_id": "a", "side": "BUY", "price": "0.5", "size": "3"}]}); self.assertEqual(books.top("a")["bid"], .5)

    def test_status_rejects_stale_pid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pid = Path(td) / "census.pid"; pid.write_text("999999999", encoding="utf-8")
            result = subprocess.run([sys.executable, "scripts/alpha_census.py", "status", "--pid", str(pid), "--db", str(Path(td) / "census.sqlite")], capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0); self.assertIn("stale pid", result.stdout)

    def test_status_output_includes_heartbeat_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "census.sqlite"
            store = CensusStore(db)
            store.initialize_status("2026-01-01T00:00:00Z")
            store.update_status("RUNNING", "running", "2026-01-01T00:00:30Z")
            store.commit()
            store.close()
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/alpha_census.py",
                    "status",
                    "--pid",
                    str(Path(td) / "missing.pid"),
                    "--db",
                    str(db),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertIn("updated_at_utc=2026-01-01T00:00:30Z", result.stdout)

    def test_running_is_persisted_only_after_first_message_and_initial_coverage(self) -> None:
        control = {"captured_at_utc": "2026-01-01T00:00:00Z", "production_db_path": "/read-only-production.sqlite", "position_count": 1, "horizon_mix": {"1": 1}, "realized_pnl_usd": 0.0, "unrealized_pnl_usd": 0.0, "deployed_capital_usd": 1.0, "opportunity_count": 1, "admission_count": 1, "status": "OK"}
        cycle = {"captured_at_utc": "2026-01-01T00:00:00Z", "cycle_id": "cycle", "cycle_timestamp_utc": "2026-01-01T00:00:00Z", "freshness_seconds": 1.0, "runner_pid": 1, "runner_state": "RUNNING", "cycle_state": "FRESH", "warning": None, "source": "test", "tolerance_seconds": 90.0}
        payload = [{"id": "event", "title": "NBA game", "markets": [{"id": "market", "question": "NBA winner", "clobTokenIds": ["asset", "other"]}]}]
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "census.sqlite"

            async def fake_consume(assets, on_message, *, stop, stats, startup_event, **_kwargs):
                self.assertEqual(read_collector_status(db)["status"], "STARTING")
                with sqlite3.connect(db) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM coverage").fetchone()[0], 0)
                await startup_event("websocket_connection", "before", {})
                stats.record("connection")
                await startup_event("websocket_connection", "after", {})
                await startup_event("subscription_construction_send", "before", {"asset_count": len(assets)})
                await startup_event("subscription_construction_send", "after", {})
                await startup_event("first_message_receipt", "before", {})
                stats.messages = 1; stats.last_message_at_utc = "2026-01-01T00:00:01Z"
                await startup_event("first_message_receipt", "after", {"messages": 1})
                await on_message({"event_type": "book", "asset_id": "asset", "bids": [{"price": "0.4", "size": "10"}], "asks": [{"price": "0.6", "size": "10"}]}, time.monotonic_ns())
                self.assertEqual(read_collector_status(db)["status"], "RUNNING")
                with sqlite3.connect(db) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM coverage").fetchone()[0], 1)
                    self.assertGreater(conn.execute("SELECT COUNT(*) FROM engine_coverage").fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT messages FROM stream_health").fetchone()[0], 1)
                stop.set()

            with patch("census.collector._read_revenue_control", return_value=control), patch("census.collector.authoritative_production_cycle", return_value=cycle), patch("census.collector.PublicGetClient.get", return_value=payload), patch("census.collector.consume_market_stream", new=fake_consume):
                asyncio.run(run_stream(db=db, pid=Path(td) / "census.pid", limit=1, duration_hours=None, log_path=Path(td) / "census.log"))
            self.assertEqual(read_collector_status(db)["status"], "STOPPED")
            with sqlite3.connect(db) as conn:
                phases = {(row[0], row[1]) for row in conn.execute("SELECT json_extract(detail_json,'$.phase'),replace(event_type,'startup_phase_','') FROM collector_events WHERE event_type LIKE 'startup_phase_%'")}
            for phase in ("production_control_capture", "gamma_bootstrap", "market_token_classification", "websocket_connection", "subscription_construction_send", "first_message_receipt", "initial_coverage_engine_persistence"):
                self.assertIn((phase, "before"), phases)
                self.assertIn((phase, "after"), phases)

    def test_first_message_timeout_fails_instead_of_reconnecting_forever(self) -> None:
        class Socket:
            async def send(self, _payload): return None
            async def recv(self): await asyncio.sleep(1.0); return "PONG"

        class Connection:
            async def __aenter__(self): return Socket()
            async def __aexit__(self, *_args): return None

        events = []

        async def record(phase, state, detail): events.append((phase, state, detail))

        async def exercise() -> None:
            with patch("websockets.asyncio.client.connect", return_value=Connection()), self.assertRaisesRegex(StreamStartupError, "first_message_receipt timed out"):
                await consume_market_stream(["asset"], lambda _message, _mono: asyncio.sleep(0), stop=asyncio.Event(), stats=StreamStats("2026-01-01T00:00:00Z"), startup_event=record, first_message_timeout=0.01)

        asyncio.run(exercise())
        self.assertIn(("first_message_receipt", "before"), [(phase, state) for phase, state, _detail in events])
        self.assertIn(("first_message_receipt", "failed"), [(phase, state) for phase, state, _detail in events])

    def test_startup_failure_is_persisted_as_failed(self) -> None:
        control = {"captured_at_utc": "2026-01-01T00:00:00Z", "production_db_path": "/read-only-production.sqlite", "position_count": 1, "horizon_mix": {"1": 1}, "realized_pnl_usd": 0.0, "unrealized_pnl_usd": 0.0, "deployed_capital_usd": 1.0, "opportunity_count": 1, "admission_count": 1, "status": "OK"}
        cycle = {"captured_at_utc": "2026-01-01T00:00:00Z", "cycle_id": "cycle", "cycle_timestamp_utc": "2026-01-01T00:00:00Z", "freshness_seconds": 1.0, "runner_pid": 1, "runner_state": "RUNNING", "cycle_state": "FRESH", "warning": None, "source": "test", "tolerance_seconds": 90.0}
        payload = [{"id": "event", "title": "NBA game", "markets": [{"id": "market", "question": "NBA winner", "clobTokenIds": ["asset", "other"]}]}]

        class Socket:
            async def send(self, _payload): return None
            async def recv(self): await asyncio.sleep(1.0); return "PONG"

        class Connection:
            async def __aenter__(self): return Socket()
            async def __aexit__(self, *_args): return None

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "census.sqlite"
            with patch("census.collector._read_revenue_control", return_value=control), patch("census.collector.authoritative_production_cycle", return_value=cycle), patch("census.collector.PublicGetClient.get", return_value=payload), patch("census.collector.FIRST_MESSAGE_TIMEOUT", 0.01), patch("websockets.asyncio.client.connect", return_value=Connection()), self.assertRaisesRegex(StreamStartupError, "first_message_receipt timed out"):
                asyncio.run(run_stream(db=db, pid=Path(td) / "census.pid", limit=1, duration_hours=None, log_path=Path(td) / "census.log"))
            status = read_collector_status(db)
            self.assertEqual(status["status"], "FAILED")
            self.assertEqual(status["phase"], "first_message_receipt")
            self.assertIn("first_message_receipt timed out", status["error"])
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT messages FROM stream_health").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM collector_events WHERE event_type='collector_failed'").fetchone()[0], 1)



class GammaPaginationTests(unittest.TestCase):

    def test_gamma_bootstrap_paginates_to_requested_target(self) -> None:
        from census.collector import _gamma_bootstrap

        class Client:
            def __init__(self):
                self.urls = []

            def get(self, url):
                import urllib.parse

                self.urls.append(url)
                query = urllib.parse.parse_qs(
                    urllib.parse.urlparse(url).query
                )
                offset = int(query["offset"][0])
                limit = int(query["limit"][0])

                return [
                    {
                        "id": str(offset + i),
                        "markets": [],
                    }
                    for i in range(limit)
                ]

        client = Client()

        rows = _gamma_bootstrap(
            client,
            requested_events=250,
        )

        self.assertEqual(len(rows), 250)
        self.assertEqual(len(client.urls), 3)

        import urllib.parse

        offsets = [
            int(
                urllib.parse.parse_qs(
                    urllib.parse.urlparse(url).query
                )["offset"][0]
            )
            for url in client.urls
        ]

        self.assertEqual(offsets, [0, 100, 200])


    def test_gamma_bootstrap_deduplicates_events(self) -> None:
        from census.collector import _gamma_bootstrap

        class Client:
            def __init__(self):
                self.calls = 0

            def get(self, _url):
                self.calls += 1

                if self.calls == 1:
                    return [
                        {"id": "1", "markets": []},
                        {"id": "2", "markets": []},
                    ]

                return [
                    {"id": "2", "markets": []},
                    {"id": "3", "markets": []},
                ]

        with patch(
            "census.collector.GAMMA_BOOTSTRAP_PAGE_SIZE",
            2,
        ):
            rows = _gamma_bootstrap(
                Client(),
                requested_events=3,
            )

        self.assertEqual(
            [row["id"] for row in rows],
            ["1", "2", "3"],
        )


    def test_gamma_bootstrap_rejects_malformed_page(self) -> None:
        from census.collector import _gamma_bootstrap

        class Client:
            def get(self, _url):
                return {"unexpected": "object"}

        with self.assertRaisesRegex(
            TypeError,
            "Gamma response was not a list",
        ):
            _gamma_bootstrap(
                Client(),
                requested_events=100,
            )


    def test_gamma_bootstrap_stops_on_short_page(self) -> None:
        from census.collector import _gamma_bootstrap

        class Client:
            def __init__(self):
                self.calls = 0

            def get(self, _url):
                self.calls += 1
                return [
                    {"id": "1", "markets": []},
                    {"id": "2", "markets": []},
                ]

        client = Client()

        with patch(
            "census.collector.GAMMA_BOOTSTRAP_PAGE_SIZE",
            100,
        ):
            rows = _gamma_bootstrap(
                client,
                requested_events=1000,
            )

        self.assertEqual(len(rows), 2)
        self.assertEqual(client.calls, 1)


    def test_larger_discovery_does_not_raise_stream_asset_cap(self) -> None:
        from census.collector import _classify_bootstrap

        payload = []

        for event_index in range(20):
            markets = []

            for market_index in range(10):
                token_base = (
                    event_index * 1000
                    + market_index * 2
                )

                markets.append(
                    {
                        "id": (
                            f"market-{event_index}-"
                            f"{market_index}"
                        ),
                        "question": "NBA winner",
                        "clobTokenIds": [
                            f"token-{token_base}",
                            f"token-{token_base + 1}",
                        ],
                    }
                )

            payload.append(
                {
                    "id": f"event-{event_index}",
                    "title": "NBA game",
                    "markets": markets,
                }
            )

        with patch(
            "census.collector.MAX_STREAM_ASSETS",
            17,
        ):
            result = _classify_bootstrap(
                payload,
                control_positions=0,
                timeout_seconds=5,
            )

        self.assertLessEqual(
            len(result.stream_assets),
            17,
        )
        self.assertEqual(
            len(result.stream_assets),
            17,
        )


if __name__ == "__main__": unittest.main()
