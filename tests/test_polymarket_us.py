from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nacl.signing import SigningKey
from polymarket_us import PolymarketUS
from polymarket_us.auth import create_auth_headers

from maker_spread_economics.live_engine import (
    ExecutionEngine,
    LiveLimits,
    LiveStore,
    OrderIntent,
    SafetyStop,
)
from maker_spread_economics.polymarket_us import (
    PMUS_KEY_ID_ENV,
    PMUS_SECRET_KEY_ENV,
    PolymarketUSPublicClient,
    PolymarketUSVenue,
    normalize_book,
    normalize_market_page,
    normalize_order,
    normalize_positions,
    redact_sensitive,
    token_id,
)
from scripts.parallax_live_maker import (
    discover_all_active_us_markets,
    parse_args,
    scan_us_candidates,
)


def raw_order(
    *,
    order_id: str = "order-1",
    state: str = "ORDER_STATE_NEW",
    cumulative: float = 0,
    average: str = "0.49",
    commission: str = "0",
    intent: str = "ORDER_INTENT_BUY_LONG",
) -> dict:
    return {
        "id": order_id,
        "marketSlug": "market-one",
        "intent": intent,
        "side": "ORDER_SIDE_BUY",
        "type": "ORDER_TYPE_LIMIT",
        "price": {"value": "0.49", "currency": "USD"},
        "quantity": 5,
        "cumQuantity": cumulative,
        "leavesQuantity": 5 - cumulative,
        "state": state,
        "avgPx": {"value": average, "currency": "USD"},
        "commissionNotionalTotalCollected": {
            "value": commission,
            "currency": "USD",
        },
        "insertTime": "2026-09-04T12:00:00Z",
    }


def raw_book() -> dict:
    return {
        "marketData": {
            "marketSlug": "market-one",
            "bids": [
                {"px": {"value": "0.49", "currency": "USD"}, "qty": "10"},
                {"px": {"value": "0.48", "currency": "USD"}, "qty": "20"},
            ],
            "offers": [
                {"px": {"value": "0.51", "currency": "USD"}, "qty": "12"}
            ],
            "state": "MARKET_STATE_OPEN",
            "stats": {
                "notionalTraded": {"value": "2400", "currency": "USD"}
            },
            "transactTime": "2026-09-04T12:00:00Z",
        }
    }


class FakeMarkets:
    def __init__(self) -> None:
        self.list_calls: list[dict] = []

    def list(self, params: dict) -> dict:
        self.list_calls.append(params)
        if params["offset"]:
            return {"markets": []}
        return {
            "markets": [
                {
                    "id": 1,
                    "slug": "market-one",
                    "question": "Will it happen?",
                    "active": True,
                    "closed": False,
                    "status": "MARKET_STATUS_OPEN",
                    "ep3Status": "OPEN",
                    "orderPriceMinTickSize": 0.01,
                    "minimumTradeQty": 1,
                    "marketSides": [{"tradable": True}],
                }
            ]
        }

    def book(self, slug: str) -> dict:
        assert slug == "market-one"
        return raw_book()


class FakeOrders:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.cancelled: list[tuple[str, dict]] = []
        self.cancel_all_calls = 0
        self.rows: dict[str, dict] = {"order-1": raw_order()}

    def create(self, params: dict) -> dict:
        self.created.append(params)
        self.rows["order-1"] = {
            **raw_order(
                intent=params["intent"],
                average=str(params["price"]["value"]),
            ),
            "marketSlug": params["marketSlug"],
            "price": dict(params["price"]),
            "quantity": params["quantity"],
            "leavesQuantity": params["quantity"],
        }
        return {"id": "order-1", "executions": []}

    def retrieve(self, order_id: str) -> dict:
        return {"order": self.rows[order_id]}

    def list(self, _params: dict | None = None) -> dict:
        return {
            "orders": [
                row
                for row in self.rows.values()
                if row["state"] in {"ORDER_STATE_NEW", "ORDER_STATE_PARTIALLY_FILLED"}
            ]
        }

    def cancel(self, order_id: str, params: dict) -> None:
        self.cancelled.append((order_id, params))
        self.rows[order_id] = {**self.rows[order_id], "state": "ORDER_STATE_CANCELED"}

    def cancel_all(self, _params: dict | None = None) -> dict:
        self.cancel_all_calls += 1
        ids = list(self.rows)
        self.rows = {
            key: {**row, "state": "ORDER_STATE_CANCELED"}
            for key, row in self.rows.items()
        }
        return {"canceledOrderIds": ids}


class FakePortfolio:
    def positions(self, _params: dict | None = None) -> dict:
        return {"positions": {}, "eof": True}

    def activities(self, _params: dict | None = None) -> dict:
        return {"activities": [], "eof": True}


class FakeAccount:
    def balances(self) -> dict:
        return {"balances": [{"currency": "USD", "currentBalance": 30.0}]}


class FakeClient:
    def __init__(self) -> None:
        self.markets = FakeMarkets()
        self.orders = FakeOrders()
        self.portfolio = FakePortfolio()
        self.account = FakeAccount()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class PolymarketUSPortTests(unittest.TestCase):
    def test_official_sdk_client_and_ed25519_signing(self) -> None:
        key_id = "00000000-0000-0000-0000-000000000001"
        secret = base64.b64encode(SigningKey.generate().encode()).decode()
        client = PolymarketUS(key_id=key_id, secret_key=secret)
        try:
            headers = create_auth_headers(key_id, secret, "GET", "/v1/orders/open")
            self.assertEqual(headers["X-PM-Access-Key"], key_id)
            self.assertTrue(headers["X-PM-Signature"])
            self.assertTrue(headers["X-PM-Timestamp"].isdigit())
        finally:
            client.close()

    def test_market_discovery_and_pagination_normalization(self) -> None:
        fake = FakeClient()
        public = PolymarketUSPublicClient(client=fake)
        rows = discover_all_active_us_markets(public, page_size=1)
        self.assertEqual([row["slug"] for row in rows], ["market-one"])
        self.assertEqual([call["offset"] for call in fake.markets.list_calls], [0, 1])
        self.assertEqual(rows[0]["tick_size"], 0.01)
        self.assertEqual(rows[0]["minimum_trade_quantity"], 1)

    def test_market_discovery_rejects_malformed_payload(self) -> None:
        with self.assertRaisesRegex(Exception, "omitted markets"):
            normalize_market_page({"data": []})

    def test_book_normalizes_yes_and_inverse_no(self) -> None:
        book = normalize_book(raw_book(), expected_slug="market-one")
        yes = book[token_id("market-one", "YES")]
        no = book[token_id("market-one", "NO")]
        self.assertEqual((yes["best_bid"], yes["best_ask"]), (0.49, 0.51))
        self.assertAlmostEqual(no["best_bid"], 0.49)
        self.assertAlmostEqual(no["best_ask"], 0.51)
        self.assertEqual(no["bid_size_shares"], 12)
        self.assertEqual(no["ask_size_shares"], 10)

    def test_candidate_ranking_reuses_live_limits(self) -> None:
        fake = FakeClient()
        public = PolymarketUSPublicClient(client=fake)
        markets = discover_all_active_us_markets(public)
        ranked = scan_us_candidates(
            public, markets, limits=LiveLimits(), required_tokens=set()
        )
        self.assertEqual({row.candidate.outcome for row in ranked}, {"YES", "NO"})
        self.assertTrue(
            all(row.quote_size_shares * row.candidate.best_bid <= 4 for row in ranked)
        )

    def test_post_only_gtc_order_and_no_price_inversion(self) -> None:
        fake = FakeClient()
        with patch.dict(os.environ, {"PARALLAX_LIVE_ENABLED": "YES"}, clear=True):
            venue = PolymarketUSVenue(live_enabled=True, client=fake)
        intent = OrderIntent(
            "market-one", "event", "market-one::NO", "NO", "BUY", 0.40, 5, 0.01, 0.1
        )
        response = venue.place_post_only(intent)
        request = fake.orders.created[0]
        self.assertEqual(request["intent"], "ORDER_INTENT_BUY_SHORT")
        self.assertEqual(request["price"]["value"], "0.6")
        self.assertEqual(request["tif"], "TIME_IN_FORCE_GOOD_TILL_CANCEL")
        self.assertIs(request["participateDontInitiate"], True)
        self.assertEqual(request["manualOrderIndicator"], "MANUAL_ORDER_INDICATOR_AUTOMATIC")
        self.assertIs(request["synchronousExecution"], False)
        self.assertTrue(response["success"])

    def test_cancel_and_cancel_all_are_verified(self) -> None:
        fake = FakeClient()
        with patch.dict(os.environ, {"PARALLAX_LIVE_ENABLED": "YES"}, clear=True):
            venue = PolymarketUSVenue(live_enabled=True, client=fake)
        venue.cancel_order("order-1")
        self.assertEqual(fake.orders.cancelled, [("order-1", {"marketSlug": "market-one"})])
        fake.orders.rows["order-2"] = raw_order(order_id="order-2")
        response = venue.cancel_all()
        self.assertEqual(response["not_canceled"], [])
        self.assertEqual(venue.get_open_orders(), [])

    def test_order_status_partial_and_full_normalization(self) -> None:
        partial = normalize_order(
            raw_order(state="ORDER_STATE_PARTIALLY_FILLED", cumulative=2)
        )
        full = normalize_order(raw_order(state="ORDER_STATE_FILLED", cumulative=5))
        self.assertEqual(partial["status"], "LIVE")
        self.assertEqual(partial["size_matched"], 2)
        self.assertEqual(full["status"], "FILLED")
        self.assertEqual(full["size_matched"], 5)

    def test_partial_full_fill_and_maker_rebate_reconcile_idempotently(self) -> None:
        temp = tempfile.TemporaryDirectory()
        try:
            store = LiveStore(
                Path(temp.name) / "live.sqlite",
                mode="LIVE",
                capital_allocated_usd=30,
            )
            intent = OrderIntent(
                "market-one", "event", "market-one::YES", "YES", "BUY", 0.49, 5, 0.01, 0.1
            )
            local = store.submit_order(intent, mode="LIVE")
            store.acknowledge_order(
                local, venue_order_id="order-1", status="RESTING", latency_ms=1, raw={}
            )
            fake = FakeClient()
            fake.orders.rows["order-1"] = raw_order(
                state="ORDER_STATE_PARTIALLY_FILLED",
                cumulative=2,
                average="0.49",
                commission="0.01",
            )
            with patch.dict(os.environ, {"PARALLAX_LIVE_ENABLED": "YES"}, clear=True):
                venue = PolymarketUSVenue(live_enabled=True, client=fake)
            venue._post_only_ids.add("order-1")
            engine = ExecutionEngine(store=store, venue=venue, limits=LiveLimits(), mode="LIVE")
            self.assertEqual(engine.reconcile(), 1)
            self.assertEqual(engine.reconcile(), 0)
            self.assertEqual(store.order_by_venue_id("order-1")["status"], "PARTIALLY_FILLED")
            fake.orders.rows["order-1"] = raw_order(
                state="ORDER_STATE_FILLED",
                cumulative=5,
                average="0.50",
                commission="0.02",
            )
            self.assertEqual(engine.reconcile(), 1)
            self.assertEqual(store.order_by_venue_id("order-1")["status"], "FILLED")
            self.assertAlmostEqual(store.inventory("market-one::YES")["quantity_shares"], 5)
            summary = store.summary(
                midpoint_by_token={"market-one::YES": 0.50},
                elapsed_hours=1,
                capital=30,
            )
            self.assertAlmostEqual(summary["confirmed maker rebates"], 0.02)
            self.assertAlmostEqual(summary["REALIZED NET P/L"], 0.02)
            store.close("test")
        finally:
            temp.cleanup()

    def test_position_normalization_uses_yes_no_inventory_tokens(self) -> None:
        rows = normalize_positions(
            {
                "positions": {
                    "yes-market": {"netPositionDecimal": "2.5", "cost": {"value": "1.25"}},
                    "no-market": {"netPosition": "-4", "cost": {"value": "1.60"}},
                }
            }
        )
        self.assertEqual(
            {(row["token_id"], row["quantity_shares"]) for row in rows},
            {("yes-market::YES", 2.5), ("no-market::NO", 4.0)},
        )

    def test_authenticated_read_only_validation_performs_no_writes(self) -> None:
        fake = FakeClient()
        venue = PolymarketUSVenue(live_enabled=False, read_only=True, client=fake)
        snapshot = venue.authenticate()
        self.assertEqual(len(snapshot["balances"]), 1)
        self.assertTrue(snapshot["cancel_all_supported"])
        self.assertEqual(fake.orders.created, [])
        self.assertEqual(fake.orders.cancel_all_calls, 0)
        with self.assertRaisesRegex(SafetyStop, "read-only"):
            venue.place_post_only(
                OrderIntent(
                    "market-one", "event", "market-one::YES", "YES", "BUY", 0.49, 1, 0.01, 0.1
                )
            )

    def test_venue_routing_and_international_live_refusal(self) -> None:
        parsed = parse_args(
            [
                "--dry-run",
                "--venue",
                "polymarket-us",
                "--db",
                "x.sqlite",
            ]
        )
        self.assertEqual(parsed.venue, "polymarket-us")
        with patch.dict(os.environ, {"PARALLAX_LIVE_ENABLED": "YES"}, clear=True):
            from scripts.parallax_live_maker import main

            with self.assertRaisesRegex(SystemExit, "international remains read-only"):
                main(
                    [
                        "--live",
                        "--venue",
                        "polymarket-international",
                        "--db",
                        "x.sqlite",
                    ]
                )

    def test_secret_redaction(self) -> None:
        with patch.dict(
            os.environ,
            {PMUS_KEY_ID_ENV: "visible-key-id", PMUS_SECRET_KEY_ENV: "top-secret"},
            clear=True,
        ):
            message = redact_sensitive("failure top-secret visible-key-id")
        self.assertNotIn("top-secret", message)
        self.assertNotIn("visible-key-id", message)
        self.assertEqual(message.count("<redacted>"), 2)


if __name__ == "__main__":
    unittest.main()
