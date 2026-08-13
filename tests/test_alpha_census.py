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
from census.storage import CensusStore
from census.stream import BookState, StreamStartupError, StreamStats, consume_market_stream


class AlphaCensusTests(unittest.TestCase):
    def test_transport_is_public_get_only_and_allowlisted(self) -> None:
        self.assertEqual(ALLOWED_HOSTS, {"gamma-api.polymarket.com", "clob.polymarket.com"})
        with self.assertRaisesRegex(ValueError, "outside public allowlist"):
            PublicGetClient().get("https://evil.example/book")

    def test_sparse_store_persists_episode_not_quote_updates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "census.sqlite"; store = CensusStore(path)
            state = SimpleNamespace(key="k", started_at_utc="2026-01-01T00:00:00Z", end_ns=2_000_000_000, started_ns=0, lifetime_seconds=2.0, best_bid=.4, best_ask=.6, best_spread=.2, worst_spread=.3, best_depth_usd=10.0, worst_depth_usd=5.0, gross_edge_usd=None, fee_source="UNKNOWN", fee_rate=None, maker_rebate_rate=None, maker_rebate_economics="UNKNOWN", net_edge_usd=None, slippage_source="UNKNOWN", executable=False, checkpoints={1.0: False, 5.0: None, 30.0: None, 60.0: None}, unknown_reason="stream_gap_timeout", quote_count=100, quote_movements=[.01], adverse_selection=[-.02], rejection_reason="fill_probability_not_measured")
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

    def test_durability_unknown_after_stream_gap_and_false_before_checkpoint(self) -> None:
        tracker = DurabilityTracker(); tracker.observe("x", 0, "2026-01-01T00:00:00Z", executable=True, bid=.4, ask=.6, depth_usd=1, movement=None, adverse_selection=None)
        tracker.observe("x", 1_000_000_000, "2026-01-01T00:00:01Z", executable=True, bid=.4, ask=.6, depth_usd=1, movement=None, adverse_selection=None)
        closed = tracker.disappear("x", 2_000_000_000, reason="stream_gap_timeout", continuous=False)
        self.assertTrue(closed.checkpoints[1.0]); self.assertFalse(closed.checkpoints[5.0]); self.assertIsNone(closed.checkpoint_reasons[5.0])

    def test_stream_stats_records_health(self) -> None:
        stats = StreamStats("2026-01-01T00:00:00Z"); stats.record("connection"); stats.record("disconnect", {"error": "x"}); stats.record("connection"); stats.record("reconnect"); stats.record("protocol_error"); stats.record("stale_stream")
        self.assertEqual((stats.connection_count, stats.reconnect_count, stats.disconnect_count, stats.protocol_error_count, stats.stale_stream_events), (2, 1, 1, 1, 1))

    def test_unknown_economics_are_explicit(self) -> None:
        self.assertEqual(_fee_metadata({})["fee_source"], "UNKNOWN")
        self.assertEqual(_fee_metadata({"feesEnabled": False})["fee_rate"], 0.0)

    def test_stream_book_updates_are_incremental(self) -> None:
        books = BookState(); self.assertEqual(books.apply({"event_type": "book", "asset_id": "a", "bids": [{"price": "0.4", "size": "10"}], "asks": [{"price": "0.6", "size": "10"}]}), {"a"})
        books.apply({"event_type": "price_change", "price_changes": [{"asset_id": "a", "side": "BUY", "price": "0.5", "size": "3"}]}); self.assertEqual(books.top("a")["bid"], .5)

    def test_status_rejects_stale_pid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pid = Path(td) / "census.pid"; pid.write_text("999999999", encoding="utf-8")
            result = subprocess.run([sys.executable, "scripts/alpha_census.py", "status", "--pid", str(pid), "--db", str(Path(td) / "census.sqlite")], capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0); self.assertIn("stale pid", result.stdout)

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


if __name__ == "__main__": unittest.main()
