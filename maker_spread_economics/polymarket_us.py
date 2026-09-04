from __future__ import annotations

import math
import os
import time
from typing import Any

from .live_engine import OrderIntent, ReconciliationError, SafetyStop

PMUS_KEY_ID_ENV = "PARALLAX_PMUS_KEY_ID"
PMUS_SECRET_KEY_ENV = "PARALLAX_PMUS_SECRET_KEY"


def _number(value: Any, *, name: str) -> float:
    if isinstance(value, dict):
        value = value.get("value")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ReconciliationError(f"invalid Polymarket US {name}") from exc
    if not math.isfinite(result):
        raise ReconciliationError(f"non-finite Polymarket US {name}")
    return result


def _amount(value: Any, *, name: str, default: float | None = None) -> float:
    if value in (None, ""):
        if default is not None:
            return default
        raise ReconciliationError(f"missing Polymarket US {name}")
    if isinstance(value, dict):
        currency = str(value.get("currency") or "USD").upper()
        if currency != "USD":
            raise ReconciliationError(f"unexpected Polymarket US {name} currency")
    return _number(value, name=name)


def token_id(market_slug: str, outcome: str) -> str:
    outcome = outcome.upper()
    if not market_slug or outcome not in {"YES", "NO"}:
        raise ReconciliationError("invalid Polymarket US market token")
    return f"{market_slug}::{outcome}"


def split_token_id(value: str) -> tuple[str, str]:
    try:
        slug, outcome = value.rsplit("::", 1)
    except ValueError as exc:
        raise ReconciliationError("invalid Polymarket US internal token") from exc
    if not slug or outcome not in {"YES", "NO"}:
        raise ReconciliationError("invalid Polymarket US internal token")
    return slug, outcome


def redact_sensitive(value: object) -> str:
    text = str(value)
    for name in (PMUS_SECRET_KEY_ENV, PMUS_KEY_ID_ENV):
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def normalize_market_page(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("markets"), list):
        raise ReconciliationError("Polymarket US market response omitted markets")
    markets: list[dict[str, Any]] = []
    for raw in payload["markets"]:
        if not isinstance(raw, dict):
            raise ReconciliationError("malformed Polymarket US market")
        slug = str(raw.get("slug") or "")
        market_id = str(raw.get("id") or slug)
        if not slug or not market_id:
            raise ReconciliationError("Polymarket US market omitted ID/slug")
        sides = raw.get("marketSides")
        side_rows = sides if isinstance(sides, list) else []
        tradable = any(
            isinstance(side, dict) and side.get("tradable") is True
            for side in side_rows
        ) if side_rows else True
        markets.append(
            {
                "id": market_id,
                "slug": slug,
                "event_id": str(raw.get("eventSlug") or market_id),
                "question": str(raw.get("question") or raw.get("title") or slug),
                "active": raw.get("active") is True,
                "closed": raw.get("closed") is True,
                "accepting_orders": (
                    raw.get("status") in (None, "", "MARKET_STATUS_OPEN")
                    and raw.get("ep3Status") in (None, "", "OPEN")
                    and tradable
                ),
                "tick_size": _number(
                    raw.get("orderPriceMinTickSize", 0.01), name="tick size"
                ),
                "minimum_trade_quantity": _number(
                    raw.get("minimumTradeQty", 1.0), name="minimum trade quantity"
                ),
                "volume": _number(raw.get("volume", 0.0), name="market volume"),
                "liquidity": _number(
                    raw.get("liquidity", 0.0), name="market liquidity"
                ),
                "raw": raw,
            }
        )
    return markets


def normalize_book(payload: Any, *, expected_slug: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ReconciliationError("malformed Polymarket US book response")
    book = payload.get("marketData", payload)
    if not isinstance(book, dict):
        raise ReconciliationError("Polymarket US book response omitted marketData")
    slug = str(book.get("marketSlug") or "")
    if slug != expected_slug:
        raise ReconciliationError("Polymarket US book market mismatch")
    state = str(book.get("state") or "")
    if state != "MARKET_STATE_OPEN":
        raise ReconciliationError(f"Polymarket US market is not open: {state or 'UNKNOWN'}")

    def levels(name: str) -> list[tuple[float, float]]:
        rows = book.get(name)
        if not isinstance(rows, list):
            raise ReconciliationError(f"Polymarket US book omitted {name}")
        result: list[tuple[float, float]] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ReconciliationError("malformed Polymarket US book level")
            price = _amount(row.get("px"), name="book price")
            quantity = _number(row.get("qty"), name="book quantity")
            if not 0 < price < 1 or quantity <= 0:
                raise ReconciliationError("invalid Polymarket US book level economics")
            result.append((price, quantity))
        return result

    bids = levels("bids")
    asks = levels("offers")
    if not bids or not asks:
        raise ReconciliationError("Polymarket US book is not two-sided")
    yes_bid = max(bids, key=lambda row: row[0])
    yes_ask = min(asks, key=lambda row: row[0])
    if yes_bid[0] >= yes_ask[0]:
        raise ReconciliationError("crossed Polymarket US book")
    observed = time.monotonic()
    return {
        token_id(slug, "YES"): {
            "best_bid": yes_bid[0],
            "best_ask": yes_ask[0],
            "bid_size_shares": yes_bid[1],
            "ask_size_shares": yes_ask[1],
            "book_observed_monotonic": observed,
        },
        token_id(slug, "NO"): {
            "best_bid": 1.0 - yes_ask[0],
            "best_ask": 1.0 - yes_bid[0],
            "bid_size_shares": yes_ask[1],
            "ask_size_shares": yes_bid[1],
            "book_observed_monotonic": observed,
        },
        "transact_time": book.get("transactTime"),
        "stats": book.get("stats") if isinstance(book.get("stats"), dict) else {},
    }


def _outcome_from_intent(intent: str) -> str:
    if intent.endswith("_LONG"):
        return "YES"
    if intent.endswith("_SHORT"):
        return "NO"
    raise ReconciliationError(f"unknown Polymarket US order intent: {intent}")


def _action_from_intent(intent: str) -> str:
    if "_BUY_" in intent:
        return "BUY"
    if "_SELL_" in intent:
        return "SELL"
    raise ReconciliationError(f"unknown Polymarket US order intent: {intent}")


def normalize_order(raw: Any, *, post_only: bool = False) -> dict[str, Any]:
    if isinstance(raw, dict) and isinstance(raw.get("order"), dict):
        raw = raw["order"]
    if not isinstance(raw, dict):
        raise ReconciliationError("malformed Polymarket US order")
    order_id = str(raw.get("id") or "")
    slug = str(raw.get("marketSlug") or "")
    intent = str(raw.get("intent") or "")
    if not order_id or not slug or not intent:
        raise ReconciliationError("Polymarket US order omitted identity")
    outcome = _outcome_from_intent(intent)
    action = _action_from_intent(intent)
    long_price = _amount(raw.get("price"), name="order price")
    average = _amount(raw.get("avgPx"), name="average fill price", default=long_price)
    quantity = _number(raw.get("quantity", 0), name="order quantity")
    cumulative = _number(raw.get("cumQuantity", 0), name="cumulative fill quantity")
    leaves = _number(
        raw.get("leavesQuantity", max(0.0, quantity - cumulative)),
        name="leaves quantity",
    )
    state = str(raw.get("state") or "")
    status_by_state = {
        "ORDER_STATE_NEW": "LIVE",
        "ORDER_STATE_PARTIALLY_FILLED": "LIVE",
        "ORDER_STATE_FILLED": "FILLED",
        "ORDER_STATE_CANCELED": "CANCELLED",
        "ORDER_STATE_REJECTED": "REJECTED",
        "ORDER_STATE_EXPIRED": "EXPIRED",
        "ORDER_STATE_REPLACED": "CANCELLED",
    }
    commission = _amount(
        raw.get("commissionNotionalTotalCollected"),
        name="commission total",
        default=0.0,
    )
    maker_bps = _number(
        raw.get("makerCommissionsBasisPoints", 0), name="maker commission bps"
    )
    is_maker = post_only or maker_bps < 0
    internal_price = long_price if outcome == "YES" else 1.0 - long_price
    internal_average = average if outcome == "YES" else 1.0 - average
    return {
        "id": order_id,
        "orderID": order_id,
        "market_id": slug,
        "marketSlug": slug,
        "token_id": token_id(slug, outcome),
        "outcome": outcome,
        "side": action,
        "price": internal_price,
        "quantity": quantity,
        "size_matched": cumulative,
        "leaves_quantity": leaves,
        "status": status_by_state.get(state, state),
        "state": state,
        "avg_fill_price": internal_average,
        "fees_total_usd": 0.0 if is_maker else max(0.0, commission),
        "maker_rebate_total_usd": max(0.0, commission) if is_maker else 0.0,
        "cumulative_fill": True,
        "timestamp": raw.get("updateTime") or raw.get("insertTime") or raw.get("createTime"),
        "raw": raw,
    }


def normalize_execution(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ReconciliationError("malformed Polymarket US execution")
    order = normalize_order(raw.get("order"), post_only=raw.get("aggressor") is False)
    kind = str(raw.get("type") or "")
    quantity = _number(raw.get("lastShares", 0), name="execution quantity")
    long_price = _amount(raw.get("lastPx"), name="execution price", default=order["price"])
    price = long_price if order["outcome"] == "YES" else 1.0 - long_price
    commission = _amount(
        raw.get("commissionNotionalCollected"), name="execution commission", default=0.0
    )
    return {
        "id": str(raw.get("id") or raw.get("tradeId") or ""),
        "status": (
            "CONFIRMED"
            if kind in {"EXECUTION_TYPE_PARTIAL_FILL", "EXECUTION_TYPE_FILL"}
            else kind
        ),
        "match_time": raw.get("transactTime") or order.get("timestamp"),
        "maker_orders": [
            {
                "order_id": order["id"],
                "matched_amount": quantity,
                "price": price,
                "fee": 0.0,
                "rebate": max(0.0, commission),
            }
        ] if raw.get("aggressor") is not True and quantity > 0 else [],
        "taker_order_id": order["id"] if raw.get("aggressor") is True else "",
        "raw": raw,
    }


def normalize_positions(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("positions"), dict):
        raise ReconciliationError("Polymarket US position response omitted positions")
    result: list[dict[str, Any]] = []
    for slug, raw in payload["positions"].items():
        if not isinstance(raw, dict):
            raise ReconciliationError("malformed Polymarket US position")
        quantity = _number(
            raw.get("netPositionDecimal", raw.get("netPosition", 0)),
            name="net position",
        )
        if abs(quantity) <= 1e-9:
            continue
        outcome = "YES" if quantity > 0 else "NO"
        absolute = abs(quantity)
        cost = _amount(raw.get("cost"), name="position cost", default=0.0)
        result.append(
            {
                "market_id": str(slug),
                "token_id": token_id(str(slug), outcome),
                "outcome": outcome,
                "quantity_shares": absolute,
                "average_cost_usd": cost / absolute if absolute else 0.0,
                "realized_pnl_usd": _amount(
                    raw.get("realized"), name="realized P/L", default=0.0
                ),
                "raw": raw,
            }
        )
    return result


class PolymarketUSPublicClient:
    def __init__(self, *, client: Any | None = None, timeout_seconds: float = 8.0) -> None:
        if client is None:
            try:
                from polymarket_us import PolymarketUS
            except ImportError as exc:
                raise SafetyStop("polymarket-us is required for Polymarket US") from exc
            client = PolymarketUS(timeout=timeout_seconds)
        self.client = client

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def markets_page(self, *, limit: int, offset: int) -> list[dict[str, Any]]:
        try:
            payload = self.client.markets.list(
                {
                    "active": True,
                    "closed": False,
                    "limit": limit,
                    "offset": offset,
                    "orderBy": ["volumeNum"],
                    "orderDirection": "desc",
                }
            )
            return normalize_market_page(payload)
        except Exception as exc:
            if isinstance(exc, (SafetyStop, ReconciliationError)):
                raise
            raise SafetyStop(
                f"Polymarket US market discovery failed: {redact_sensitive(exc)}"
            ) from exc

    def book(self, slug: str) -> dict[str, Any]:
        try:
            return normalize_book(self.client.markets.book(slug), expected_slug=slug)
        except Exception as exc:
            if isinstance(exc, (SafetyStop, ReconciliationError)):
                raise
            raise SafetyStop(
                f"Polymarket US book retrieval failed for {slug}: {redact_sensitive(exc)}"
            ) from exc


class PolymarketUSVenue:
    REQUIRED_ENV = (PMUS_KEY_ID_ENV, PMUS_SECRET_KEY_ENV)

    def __init__(
        self,
        *,
        live_enabled: bool,
        read_only: bool = False,
        client: Any | None = None,
    ) -> None:
        from .live_engine import LIVE_ENABLED_VALUE, LIVE_ENV

        if live_enabled == read_only:
            raise ValueError("choose exactly one of live_enabled or read_only")
        if live_enabled and os.environ.get(LIVE_ENV) != LIVE_ENABLED_VALUE:
            raise SafetyStop(
                f"live mode refused: {LIVE_ENV} must equal {LIVE_ENABLED_VALUE}"
            )
        missing = [name for name in self.REQUIRED_ENV if not os.environ.get(name)]
        if client is None and missing:
            raise SafetyStop(
                f"missing Polymarket US authentication variables: {', '.join(missing)}"
            )
        if client is None:
            try:
                from polymarket_us import PolymarketUS
            except ImportError as exc:
                raise SafetyStop(
                    "polymarket-us is required for authenticated Polymarket US access"
                ) from exc
            client = PolymarketUS(
                key_id=os.environ[PMUS_KEY_ID_ENV],
                secret_key=os.environ[PMUS_SECRET_KEY_ENV],
                timeout=8.0,
            )
        self.client = client
        self.read_only = read_only
        self._post_only_ids: set[str] = set()
        self._slug_by_order: dict[str, str] = {}

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def authenticate(self, *, allow_closed_only: bool = False) -> dict[str, Any]:
        del allow_closed_only
        try:
            balances = self.client.account.balances()
            orders = self.client.orders.list()
            positions = self.client.portfolio.positions()
            activities = self.client.portfolio.activities(
                {"limit": 100, "types": ["ACTIVITY_TYPE_TRADE"]}
            )
            if not isinstance(balances, dict) or not isinstance(
                balances.get("balances"), list
            ):
                raise ReconciliationError("Polymarket US balances response is malformed")
            normalized_orders = self._normalize_order_list(orders)
            normalized_positions = normalize_positions(positions)
            executions = self._normalize_activities(activities)
            cancel_all_supported = callable(
                getattr(self.client.orders, "cancel_all", None)
            )
            if not cancel_all_supported:
                raise ReconciliationError("Polymarket US cancel-all capability is absent")
            return {
                "balances": balances["balances"],
                "open_orders": normalized_orders,
                "positions": normalized_positions,
                "executions": executions,
                "cancel_all_supported": True,
            }
        except Exception as exc:
            if isinstance(exc, SafetyStop):
                raise
            raise SafetyStop(
                f"Polymarket US authentication/reconciliation failed: {redact_sensitive(exc)}"
            ) from exc

    @staticmethod
    def _normalize_activities(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("activities"), list):
            raise ReconciliationError("Polymarket US activities response is malformed")
        result: list[dict[str, Any]] = []
        for row in payload["activities"]:
            if not isinstance(row, dict) or row.get("type") != "ACTIVITY_TYPE_TRADE":
                continue
            trade = row.get("trade")
            if not isinstance(trade, dict) or not trade.get("id"):
                raise ReconciliationError("malformed Polymarket US trade activity")
            result.append(
                {
                    "id": str(trade["id"]),
                    "market_id": str(trade.get("marketSlug") or ""),
                    "status": str(trade.get("state") or ""),
                    "timestamp": trade.get("updateTime") or trade.get("createTime"),
                    "price": _amount(trade.get("price"), name="trade price"),
                    "quantity": _number(trade.get("qty"), name="trade quantity"),
                    "is_aggressor": trade.get("isAggressor"),
                    "realized_pnl_usd": _amount(
                        trade.get("realizedPnl"), name="trade realized P/L", default=0.0
                    ),
                    "raw": trade,
                }
            )
        return result

    def _normalize_order_list(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("orders"), list):
            raise ReconciliationError("Polymarket US open-order response is malformed")
        result = []
        for row in payload["orders"]:
            order_id = str(row.get("id") or "") if isinstance(row, dict) else ""
            normalized = normalize_order(
                row, post_only=bool(order_id and order_id in self._post_only_ids)
            )
            self._slug_by_order[normalized["id"]] = normalized["marketSlug"]
            result.append(normalized)
        return result

    def place_post_only(self, intent: OrderIntent) -> dict[str, Any]:
        if self.read_only:
            raise SafetyStop("read-only Polymarket US client cannot submit orders")
        slug, encoded_outcome = split_token_id(intent.token_id)
        outcome = intent.outcome.upper()
        if encoded_outcome != outcome or intent.market_id != slug:
            raise ReconciliationError("Polymarket US order intent identity mismatch")
        long_price = intent.price if outcome == "YES" else 1.0 - intent.price
        order_intent = {
            ("YES", "BUY"): "ORDER_INTENT_BUY_LONG",
            ("YES", "SELL"): "ORDER_INTENT_SELL_LONG",
            ("NO", "BUY"): "ORDER_INTENT_BUY_SHORT",
            ("NO", "SELL"): "ORDER_INTENT_SELL_SHORT",
        }.get((outcome, intent.side))
        if order_intent is None:
            raise ReconciliationError("invalid Polymarket US order side/outcome")
        if not 0.01 <= long_price <= 0.99:
            raise SafetyStop("Polymarket US long-side order price is out of bounds")
        params = {
            "marketSlug": slug,
            "intent": order_intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{long_price:.10f}".rstrip("0").rstrip("."), "currency": "USD"},
            "quantity": intent.size_shares,
            "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
            "participateDontInitiate": True,
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            "synchronousExecution": False,
        }
        response = self.client.orders.create(params)
        order_id = str(response.get("id") or "") if isinstance(response, dict) else ""
        if not order_id:
            raise ReconciliationError("Polymarket US order response omitted order ID")
        self._post_only_ids.add(order_id)
        self._slug_by_order[order_id] = slug
        deadline = time.monotonic() + 2.0
        normalized: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            normalized = normalize_order(
                self.client.orders.retrieve(order_id), post_only=True
            )
            if normalized["state"] not in {
                "ORDER_STATE_PENDING_NEW",
                "ORDER_STATE_PENDING_RISK",
            }:
                break
            time.sleep(0.1)
        if normalized is None:
            raise ReconciliationError("Polymarket US order state unavailable")
        if (
            normalized["market_id"] != intent.market_id
            or normalized["token_id"] != intent.token_id
            or normalized["outcome"] != outcome
            or normalized["side"] != intent.side
            or abs(float(normalized["price"]) - intent.price) > intent.tick_size / 2 + 1e-9
            or abs(float(normalized["quantity"]) - intent.size_shares) > 1e-9
        ):
            raise ReconciliationError("Polymarket US acknowledged order does not match intent")
        success = normalized["state"] == "ORDER_STATE_NEW"
        return {
            **normalized,
            "success": success,
            "status": "LIVE" if success else normalized["status"],
            "errorMsg": None if success else f"order state {normalized['state']}",
        }

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        slug = self._slug_by_order.get(order_id)
        if not slug:
            current = normalize_order(self.client.orders.retrieve(order_id))
            slug = current["marketSlug"]
            self._slug_by_order[order_id] = slug
        self.client.orders.cancel(order_id, {"marketSlug": slug})
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            state = normalize_order(
                self.client.orders.retrieve(order_id),
                post_only=order_id in self._post_only_ids,
            )["state"]
            if state == "ORDER_STATE_CANCELED":
                return {"canceled": [order_id], "not_canceled": []}
            if state in {"ORDER_STATE_FILLED", "ORDER_STATE_REJECTED", "ORDER_STATE_EXPIRED"}:
                break
            time.sleep(0.1)
        raise ReconciliationError(f"uncertain cancellation state for order {order_id}")

    def cancel_all(self) -> dict[str, Any]:
        response = self.client.orders.cancel_all()
        deadline = time.monotonic() + 3.0
        remaining: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            remaining = self.get_open_orders()
            if not remaining:
                canceled = response.get("canceledOrderIds", []) if isinstance(response, dict) else []
                return {"canceled": canceled, "not_canceled": []}
            time.sleep(0.1)
        return {
            "canceled": response.get("canceledOrderIds", []) if isinstance(response, dict) else [],
            "not_canceled": [row["id"] for row in remaining],
        }

    def get_open_orders(self) -> list[dict[str, Any]]:
        return self._normalize_order_list(self.client.orders.list())

    def get_order(self, order_id: str) -> dict[str, Any]:
        normalized = normalize_order(
            self.client.orders.retrieve(order_id),
            post_only=order_id in self._post_only_ids,
        )
        self._slug_by_order[order_id] = normalized["marketSlug"]
        return normalized

    def get_trades(self, *, after: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "limit": 100,
            "types": ["ACTIVITY_TYPE_TRADE"],
            "sortOrder": "SORT_ORDER_DESCENDING",
        }
        if after is not None:
            params["cursor"] = str(after)
        return self._normalize_activities(self.client.portfolio.activities(params))

    def get_positions(self) -> list[dict[str, Any]]:
        return normalize_positions(self.client.portfolio.positions())

    def heartbeat(self, heartbeat_id: str) -> dict[str, Any]:
        del heartbeat_id
        payload = self.client.account.balances()
        if not isinstance(payload, dict) or not isinstance(payload.get("balances"), list):
            raise ReconciliationError("Polymarket US authentication heartbeat failed")
        return {"heartbeat_id": str(time.time_ns())}

    def confirmed_rewards(self, date: str) -> list[dict[str, Any]]:
        payload = self.client.get(
            "/v1/incentives/earnings",
            query={"start_date": date, "end_date": date},
            authenticated=True,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("rewards"), list):
            raise ReconciliationError("Polymarket US incentive earnings response is malformed")
        result = []
        for row in payload["rewards"]:
            if not isinstance(row, dict):
                raise ReconciliationError("malformed Polymarket US incentive earning")
            result.append(
                {
                    "condition_id": str(row.get("marketSlug") or ""),
                    "earnings": _number(row.get("reward", 0), name="incentive reward"),
                    "asset_rate": 1.0,
                    "program_type": str(row.get("programType") or ""),
                    "status": str(row.get("status") or ""),
                    "date": str(row.get("date") or date),
                    "raw": row,
                }
            )
        return result
