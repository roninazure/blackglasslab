from __future__ import annotations

import io
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from maker_spread_economics.live import PublicHTTPError, ReadOnlyPublicClient
from maker_spread_economics.live_engine import (
    Candidate,
    ExecutionEngine,
    LiveLimits,
    LiveStore,
    OrderIntent,
    PolymarketVenue,
    ProcessLock,
    RankedCandidate,
    ReconciliationError,
    SafetyStop,
    rank_candidate,
    require_geographic_eligibility,
)
from scripts.parallax_live_maker import discover_all_active_markets


class FakeVenue:
    def __init__(self) -> None:
        self.open_orders: list[dict] = []
        self.trades: list[dict] = []
        self.terminal: dict[str, dict] = {}
        self.cancel_all_calls = 0
        self.cancel_calls: list[str] = []
        self.place_calls: list[OrderIntent] = []
        self.place_response = {"success": True, "orderID": "venue-1", "status": "live"}
        self.place_error: Exception | None = None
        self.reconcile_error: Exception | None = None

    def place_post_only(self, intent: OrderIntent) -> dict:
        self.place_calls.append(intent)
        if self.place_error:
            raise self.place_error
        return dict(self.place_response)

    def cancel_order(self, order_id: str) -> dict:
        self.cancel_calls.append(order_id)
        self.open_orders = [
            row for row in self.open_orders if row.get("id") != order_id
        ]
        return {"canceled": [order_id]}

    def cancel_all(self) -> dict:
        self.cancel_all_calls += 1
        self.open_orders.clear()
        return {"canceled": True}

    def get_open_orders(self) -> list[dict]:
        if self.reconcile_error:
            raise self.reconcile_error
        return list(self.open_orders)

    def get_order(self, order_id: str) -> dict:
        if order_id not in self.terminal:
            raise LookupError(order_id)
        return self.terminal[order_id]

    def get_trades(self, *, after: int | None = None) -> list[dict]:
        if self.reconcile_error:
            raise self.reconcile_error
        return list(self.trades)

    def heartbeat(self, heartbeat_id: str) -> dict:
        return {"heartbeat_id": f"next-{heartbeat_id}"}

    def confirmed_rewards(self, date: str) -> list[dict]:
        return []


def candidate(
    *, token: str = "token", market: str = "market", observed: float | None = None
) -> Candidate:
    return Candidate(
        market_id=market,
        event_id=f"event-{market}",
        token_id=token,
        outcome="YES",
        question="fixture",
        best_bid=0.49,
        best_ask=0.51,
        bid_size_shares=1.0,
        ask_size_shares=1.0,
        tick_size=0.01,
        min_order_size_shares=5.0,
        volume_24h_usd=2400.0,
        liquidity_usd=100.0,
        book_observed_monotonic=time.monotonic() if observed is None else observed,
    )


class ParallaxLiveMakerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "live.sqlite"
        self.limits = LiveLimits()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def store(self, mode: str = "DRY_RUN") -> LiveStore:
        return LiveStore(
            self.path, mode=mode, capital_allocated_usd=self.limits.total_bankroll_usd
        )

    def ranked(self, item: Candidate | None = None) -> RankedCandidate:
        result = rank_candidate(item or candidate(), self.limits)
        self.assertIsNotNone(result)
        return result  # type: ignore[return-value]

    def acknowledged_order(
        self,
        store: LiveStore,
        *,
        order_id: str,
        side: str = "BUY",
        price: float = 0.49,
        size: float = 5.0,
        token: str = "token",
        market: str = "market",
    ):
        intent = OrderIntent(
            market, f"event-{market}", token, "YES", side, price, size, 0.01, 0.1
        )
        local = store.submit_order(intent, mode="LIVE")
        store.acknowledge_order(
            local, venue_order_id=order_id, status="RESTING", latency_ms=1, raw={}
        )
        return store.order_by_venue_id(order_id)

    def fill(
        self,
        store: LiveStore,
        *,
        key: str,
        order,
        price: float,
        quantity: float,
        fees: float = 0.0,
    ) -> None:
        store.record_fill(
            external_fill_key=key,
            order=order,
            timestamp_utc="2026-09-04T12:00:00Z",
            fill_price=price,
            quantity=quantity,
            fees_usd=fees,
            raw={"id": key},
        )

    def test_order_sizing_obeys_per_order_notional_and_minimum_size(self) -> None:
        store = self.store()
        engine = ExecutionEngine(
            store=store, venue=None, limits=self.limits, mode="DRY_RUN"
        )
        intent = engine.size_intent(self.ranked(), side="BUY")
        self.assertIsNotNone(intent)
        self.assertLessEqual(intent.notional_usd, self.limits.per_order_usd)
        self.assertGreaterEqual(intent.size_shares, 5)
        store.close("test")

    def test_gamma_discovery_uses_keyset_cursor_without_offset(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.urls: list[str] = []

            def get(self, url: str, *, operation: str = ""):
                self.urls.append(url)
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                self.assert_operation = operation
                if "after_cursor" not in query:
                    return {
                        "events": [
                            {
                                "id": "event-1",
                                "title": "event",
                                "markets": [{"id": "market-1"}],
                            }
                        ],
                        "next_cursor": "cursor-2",
                    }
                return {"events": [], "next_cursor": None}

        client = Client()
        markets = discover_all_active_markets(client)  # type: ignore[arg-type]
        self.assertEqual(len(markets), 1)
        self.assertEqual(client.assert_operation, "active universe discovery")
        self.assertTrue(
            all(
                urllib.parse.urlsplit(url).path == "/events/keyset"
                for url in client.urls
            )
        )
        self.assertTrue(
            all(
                "offset"
                not in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                for url in client.urls
            )
        )
        self.assertEqual(
            urllib.parse.parse_qs(urllib.parse.urlsplit(client.urls[1]).query)[
                "after_cursor"
            ],
            ["cursor-2"],
        )

    def test_public_http_error_has_bounded_context_and_redacts_secrets(self) -> None:
        class Opener:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(
                    request.full_url,
                    422,
                    "Unprocessable Entity",
                    {},
                    io.BytesIO(b'{"error":"bad request"}'),
                )

        client = ReadOnlyPublicClient()
        with (
            patch(
                "maker_spread_economics.live.urllib.request.build_opener",
                return_value=Opener(),
            ),
            self.assertRaises(PublicHTTPError) as raised,
        ):
            client.get(
                "https://gamma-api.polymarket.com/events?api_key=secret&offset=2100",
                operation="active universe discovery",
            )
        message = str(raised.exception)
        self.assertIn("operation=active universe discovery", message)
        self.assertIn("method=GET", message)
        self.assertIn("status=422", message)
        self.assertIn('response={"error":"bad request"}', message)
        self.assertIn("api_key=%3Credacted%3E", message)
        self.assertNotIn("api_key=secret", message)

    def test_dry_run_summary_reports_zero_live_orders_and_deployment(self) -> None:
        store = self.store()
        engine = ExecutionEngine(
            store=store, venue=None, limits=self.limits, mode="DRY_RUN"
        )
        engine.place(engine.size_intent(self.ranked(), side="BUY"))  # type: ignore[arg-type]
        summary = store.summary(
            midpoint_by_token={}, elapsed_hours=1.0, capital=30.0
        )
        self.assertEqual(summary["open orders"], 0)
        self.assertEqual(summary["capital currently deployed"], 0)
        store.close("test")

    def test_sdk_adapter_forces_post_only_gtc(self) -> None:
        class Client:
            def create_and_post_order(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs
                return {"success": True, "orderID": "one", "status": "live"}

        venue = object.__new__(PolymarketVenue)
        venue.client = Client()
        intent = OrderIntent("m", "e", "t", "YES", "BUY", 0.49, 5, 0.01, 0.1)
        venue.place_post_only(intent)
        self.assertIs(venue.client.kwargs["post_only"], True)
        self.assertEqual(str(venue.client.kwargs["order_type"]), "GTC")

    def test_per_market_and_total_cap_prevent_additional_order(self) -> None:
        limits = LiveLimits(
            per_market_usd=4, per_order_usd=4, max_inventory_usd_per_market=4
        )
        store = self.store()
        engine = ExecutionEngine(store=store, venue=None, limits=limits, mode="DRY_RUN")
        first = engine.size_intent(self.ranked(), side="BUY")
        self.assertIsNotNone(first)
        engine.place(first)
        self.assertIsNone(engine.size_intent(self.ranked(), side="BUY"))
        store.close("test")

    def test_total_deployed_cap_spans_markets(self) -> None:
        limits = LiveLimits(
            max_deployed_usd=6,
            per_market_usd=6,
            per_order_usd=4,
            max_inventory_usd_per_market=6,
        )
        store = self.store()
        engine = ExecutionEngine(store=store, venue=None, limits=limits, mode="DRY_RUN")
        engine.place(engine.size_intent(self.ranked(), side="BUY"))  # type: ignore[arg-type]
        second = self.ranked(candidate(token="t2", market="m2"))
        engine.place(engine.size_intent(second, side="BUY"))  # type: ignore[arg-type]
        third = self.ranked(candidate(token="t3", market="m3"))
        self.assertIsNone(engine.size_intent(third, side="BUY"))
        store.close("test")

    def test_inventory_cap_blocks_new_buy(self) -> None:
        limits = LiveLimits(max_inventory_usd_per_market=3)
        store = self.store()
        order = self.acknowledged_order(store, order_id="buy")
        self.fill(store, key="fill-buy", order=order, price=0.5, quantity=6)
        engine = ExecutionEngine(store=store, venue=None, limits=limits, mode="DRY_RUN")
        self.assertIsNone(engine.size_intent(self.ranked(), side="BUY"))
        store.close("test")

    def test_simultaneous_market_cap_is_hard(self) -> None:
        limits = LiveLimits(max_active_markets=1)
        store = self.store()
        engine = ExecutionEngine(store=store, venue=None, limits=limits, mode="DRY_RUN")
        engine.place(engine.size_intent(self.ranked(), side="BUY"))  # type: ignore[arg-type]
        second = self.ranked(candidate(token="t2", market="m2"))
        intent = OrderIntent(
            "m2",
            "e2",
            "t2",
            "YES",
            "BUY",
            0.49,
            5,
            0.01,
            second.expected_net_usd_per_hour,
        )
        with self.assertRaisesRegex(SafetyStop, "simultaneous"):
            engine.place(intent)
        store.close("test")

    def test_stale_book_cancels_resting_quote(self) -> None:
        store = self.store()
        engine = ExecutionEngine(
            store=store, venue=None, limits=self.limits, mode="DRY_RUN"
        )
        intent = engine.size_intent(self.ranked(), side="BUY")
        engine.place(intent)  # type: ignore[arg-type]
        stale = candidate(observed=time.monotonic() - 100)
        self.assertEqual(
            engine.cancel_stale_or_moved(
                {"token": stale}, now_monotonic=time.monotonic()
            ),
            1,
        )
        self.assertEqual(store.open_orders(), [])
        store.close("test")

    def test_material_book_move_cancels_quote(self) -> None:
        store = self.store()
        engine = ExecutionEngine(
            store=store, venue=None, limits=self.limits, mode="DRY_RUN"
        )
        engine.place(engine.size_intent(self.ranked(), side="BUY"))  # type: ignore[arg-type]
        moved = candidate()
        moved = Candidate(**{**moved.__dict__, "best_bid": 0.48, "best_ask": 0.51})
        self.assertEqual(
            engine.cancel_stale_or_moved(
                {"token": moved}, now_monotonic=time.monotonic()
            ),
            1,
        )
        store.close("test")

    def test_inventory_change_cancels_unsupported_ask(self) -> None:
        store = self.store("LIVE")
        buy = self.acknowledged_order(store, order_id="buy", side="BUY", price=0.49)
        self.fill(store, key="b", order=buy, price=0.49, quantity=5)
        store.set_order_state("buy", "FILLED", 5)
        sell = self.acknowledged_order(store, order_id="sell", side="SELL", price=0.51)
        self.fill(store, key="s", order=sell, price=0.51, quantity=5)
        store.set_order_state("sell", "FILLED", 5)
        dry = ExecutionEngine(
            store=store, venue=None, limits=self.limits, mode="DRY_RUN"
        )
        store.submit_order(
            OrderIntent(
                "market", "event-market", "token", "YES", "SELL", 0.51, 5, 0.01, 0.1
            ),
            mode="DRY_RUN",
        )
        self.assertEqual(
            dry.cancel_stale_or_moved(
                {"token": candidate()}, now_monotonic=time.monotonic()
            ),
            1,
        )
        store.close("test")

    def test_websocket_failure_cancels_all_and_stops(self) -> None:
        store = self.store("LIVE")
        venue = FakeVenue()
        engine = ExecutionEngine(
            store=store, venue=venue, limits=self.limits, mode="LIVE"
        )
        with self.assertRaisesRegex(SafetyStop, "websocket"):
            engine.check_risk(
                midpoint_by_token={},
                websocket_last_message_monotonic=time.monotonic(),
                websocket_failed=True,
                now_monotonic=time.monotonic(),
            )
        self.assertEqual(venue.cancel_all_calls, 1)
        store.close("test")

    def test_authentication_gate_refuses_without_explicit_live_enable(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(SafetyStop, "PARALLAX_LIVE_ENABLED"),
        ):
            PolymarketVenue(live_enabled=True)

    def test_geographic_restriction_fails_closed(self) -> None:
        require_geographic_eligibility(
            {"blocked": False, "country": "ZZ", "region": ""}
        )
        with self.assertRaisesRegex(SafetyStop, "US/NY"):
            require_geographic_eligibility(
                {"blocked": True, "country": "US", "region": "NY"}
            )
        with self.assertRaisesRegex(SafetyStop, "unknown"):
            require_geographic_eligibility({"country": "ZZ"})

    def test_order_rejection_is_fail_closed(self) -> None:
        store = self.store("LIVE")
        venue = FakeVenue()
        venue.place_response = {
            "success": False,
            "errorMsg": "crosses book",
            "status": "rejected",
        }
        engine = ExecutionEngine(
            store=store, venue=venue, limits=self.limits, mode="LIVE"
        )
        with self.assertRaisesRegex(SafetyStop, "did not rest"):
            engine.place(engine.size_intent(self.ranked(), side="BUY"))  # type: ignore[arg-type]
        self.assertEqual(venue.cancel_all_calls, 1)
        self.assertEqual(
            store.conn.execute("SELECT status FROM live_orders").fetchone()[0],
            "REJECTED",
        )
        store.close("test")

    def test_partial_and_full_fill_reconciliation_is_idempotent(self) -> None:
        store = self.store("LIVE")
        venue = FakeVenue()
        engine = ExecutionEngine(
            store=store, venue=venue, limits=self.limits, mode="LIVE"
        )
        self.acknowledged_order(store, order_id="buy", size=5)
        venue.open_orders = [{"id": "buy", "size_matched": "2"}]
        venue.trades = [
            {
                "id": "trade-1",
                "status": "TRADE_STATUS_CONFIRMED",
                "match_time": "1788523200",
                "maker_orders": [
                    {"order_id": "buy", "matched_amount": "2", "price": "0.49"}
                ],
            }
        ]
        self.assertEqual(engine.reconcile(), 1)
        self.assertEqual(engine.reconcile(), 0)
        self.assertEqual(store.order_by_venue_id("buy")["status"], "PARTIALLY_FILLED")
        venue.open_orders = []
        venue.trades.append(
            {
                "id": "trade-2",
                "status": "TRADE_STATUS_CONFIRMED",
                "match_time": "1788523201",
                "maker_orders": [
                    {"order_id": "buy", "matched_amount": "3", "price": "0.49"}
                ],
            }
        )
        venue.terminal["buy"] = {"status": "FILLED", "size_matched": "5"}
        self.assertEqual(engine.reconcile(), 1)
        self.assertEqual(store.order_by_venue_id("buy")["status"], "FILLED")
        self.assertAlmostEqual(store.inventory("token")["quantity_shares"], 5)
        store.close("test")

    def test_fill_to_realized_net_pnl_uses_actual_round_trip_and_fees(self) -> None:
        store = self.store("LIVE")
        buy = self.acknowledged_order(store, order_id="buy", side="BUY", price=0.49)
        self.fill(store, key="b", order=buy, price=0.49, quantity=5, fees=0)
        sell = self.acknowledged_order(store, order_id="sell", side="SELL", price=0.51)
        self.fill(store, key="s", order=sell, price=0.51, quantity=5, fees=0.01)
        self.assertAlmostEqual(store.realized_net(), 0.09)
        row = store.conn.execute(
            "SELECT * FROM live_fills WHERE external_fill_key='s'"
        ).fetchone()
        self.assertAlmostEqual(row["realized_trading_pnl_usd"], 0.10)
        self.assertAlmostEqual(row["cumulative_realized_net_pnl_usd"], 0.09)
        self.assertEqual(row["inventory_after_shares"], 0)
        store.close("test")

    def test_sell_fill_larger_than_inventory_is_unknown_state(self) -> None:
        store = self.store("LIVE")
        sell = self.acknowledged_order(store, order_id="sell", side="SELL")
        with self.assertRaisesRegex(ReconciliationError, "exceeds"):
            self.fill(store, key="s", order=sell, price=0.51, quantity=5)
        store.close("test")

    def test_daily_loss_cap_triggers_cancel_all(self) -> None:
        limits = LiveLimits(max_daily_loss_usd=0.1)
        store = self.store("LIVE")
        buy = self.acknowledged_order(store, order_id="buy", side="BUY", price=0.5)
        self.fill(store, key="b", order=buy, price=0.5, quantity=5)
        sell = self.acknowledged_order(store, order_id="sell", side="SELL", price=0.4)
        self.fill(store, key="s", order=sell, price=0.4, quantity=5)
        venue = FakeVenue()
        engine = ExecutionEngine(store=store, venue=venue, limits=limits, mode="LIVE")
        with patch("maker_spread_economics.live_engine.datetime") as clock:
            clock.now.return_value = __import__("datetime").datetime(
                2026, 9, 4, tzinfo=__import__("datetime").timezone.utc
            )
            clock.side_effect = lambda *a, **kw: __import__("datetime").datetime(
                *a, **kw
            )
            with self.assertRaisesRegex(SafetyStop, "daily"):
                engine.check_risk(
                    midpoint_by_token={},
                    websocket_last_message_monotonic=time.monotonic(),
                    websocket_failed=False,
                    now_monotonic=time.monotonic(),
                )
        self.assertEqual(venue.cancel_all_calls, 1)
        store.close("test")

    def test_drawdown_cap_includes_unrealized_inventory(self) -> None:
        limits = LiveLimits(max_drawdown_usd=0.5)
        store = self.store("LIVE")
        buy = self.acknowledged_order(store, order_id="buy", side="BUY", price=0.5)
        self.fill(store, key="b", order=buy, price=0.5, quantity=5)
        store.mark_equity({"token": 0.7})
        venue = FakeVenue()
        engine = ExecutionEngine(store=store, venue=venue, limits=limits, mode="LIVE")
        with self.assertRaisesRegex(SafetyStop, "drawdown"):
            engine.check_risk(
                midpoint_by_token={"token": 0.5},
                websocket_last_message_monotonic=time.monotonic(),
                websocket_failed=False,
                now_monotonic=time.monotonic(),
            )
        self.assertEqual(venue.cancel_all_calls, 1)
        store.close("test")

    def test_unknown_venue_order_causes_reconciliation_cancel_all(self) -> None:
        store = self.store("LIVE")
        venue = FakeVenue()
        venue.open_orders = [{"id": "foreign", "size_matched": "0"}]
        engine = ExecutionEngine(
            store=store, venue=venue, limits=self.limits, mode="LIVE"
        )
        with self.assertRaisesRegex(ReconciliationError, "unknown venue"):
            engine.reconcile()
        self.assertEqual(venue.cancel_all_calls, 1)
        store.close("test")

    def test_reconciliation_retrieval_failure_is_fail_closed(self) -> None:
        store = self.store("LIVE")
        venue = FakeVenue()
        venue.reconcile_error = PermissionError("auth")
        engine = ExecutionEngine(
            store=store, venue=venue, limits=self.limits, mode="LIVE"
        )
        with self.assertRaisesRegex(SafetyStop, "CRITICAL"):
            engine.reconcile()
        self.assertEqual(venue.cancel_all_calls, 1)
        store.close("test")

    def test_global_cancel_all_marks_local_orders_cancelled(self) -> None:
        store = self.store("LIVE")
        self.acknowledged_order(store, order_id="one")
        venue = FakeVenue()
        engine = ExecutionEngine(
            store=store, venue=venue, limits=self.limits, mode="LIVE"
        )
        engine.emergency_stop("operator")
        self.assertEqual(venue.cancel_all_calls, 1)
        self.assertEqual(store.open_orders(), [])
        store.close("test")

    def test_rate_limit_before_acknowledgement_skips_cancel_all(self) -> None:
        store = self.store("LIVE")
        venue = FakeVenue()
        engine = ExecutionEngine(
            store=store, venue=venue, limits=self.limits, mode="LIVE"
        )
        engine.emergency_stop("authenticated REST rate limit")
        self.assertEqual(venue.cancel_all_calls, 0)
        store.close("test")

    def test_known_live_order_cancel_failure_is_not_recursive(self) -> None:
        store = self.store("LIVE")
        self.acknowledged_order(store, order_id="known")
        venue = FakeVenue()
        venue.reconcile_error = RuntimeError("Cloudflare Error 1015")
        engine = ExecutionEngine(
            store=store, venue=venue, limits=self.limits, mode="LIVE"
        )
        with self.assertRaisesRegex(SafetyStop, "global cancel-all failed"):
            engine.emergency_stop("authenticated REST rate limit")
        engine.emergency_stop("outer process boundary")
        self.assertEqual(venue.cancel_all_calls, 1)
        store.close("test")

    def test_environment_kill_switch_cancels_all(self) -> None:
        store = self.store("LIVE")
        venue = FakeVenue()
        engine = ExecutionEngine(
            store=store, venue=venue, limits=self.limits, mode="LIVE"
        )
        with (
            patch.dict(os.environ, {"PARALLAX_KILL_SWITCH": "YES"}),
            self.assertRaisesRegex(SafetyStop, "kill switch"),
        ):
            engine.check_risk(
                midpoint_by_token={},
                websocket_last_message_monotonic=time.monotonic(),
                websocket_failed=False,
                now_monotonic=time.monotonic(),
            )
        self.assertEqual(venue.cancel_all_calls, 1)
        store.close("test")

    def test_runtime_kill_switch_file_cancels_all(self) -> None:
        store = self.store("LIVE")
        venue = FakeVenue()
        engine = ExecutionEngine(
            store=store, venue=venue, limits=self.limits, mode="LIVE"
        )
        kill_file = Path(self.temp.name) / "kill"
        kill_file.touch()
        with (
            patch.dict(os.environ, {"PARALLAX_KILL_SWITCH_FILE": str(kill_file)}),
            self.assertRaisesRegex(SafetyStop, "kill switch"),
        ):
            engine.check_risk(
                midpoint_by_token={},
                websocket_last_message_monotonic=time.monotonic(),
                websocket_failed=False,
                now_monotonic=time.monotonic(),
            )
        self.assertEqual(venue.cancel_all_calls, 1)
        store.close("test")

    def test_duplicate_process_lock_is_refused(self) -> None:
        lock_path = Path(self.temp.name) / "runner.lock"
        with (
            ProcessLock(lock_path),
            self.assertRaisesRegex(SafetyStop, "duplicate"),
            ProcessLock(lock_path),
        ):
            pass

    def test_dry_run_never_calls_venue_write(self) -> None:
        store = self.store()
        venue = FakeVenue()
        engine = ExecutionEngine(
            store=store, venue=venue, limits=self.limits, mode="DRY_RUN"
        )
        engine.place(engine.size_intent(self.ranked(), side="BUY"))  # type: ignore[arg-type]
        engine.emergency_stop("test")
        self.assertEqual(venue.place_calls, [])
        self.assertEqual(venue.cancel_all_calls, 0)
        store.close("test")


if __name__ == "__main__":
    unittest.main()
