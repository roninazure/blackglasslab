from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class StreamStartupError(RuntimeError):
    """The public market stream did not become live inside its startup bounds."""


class BookState:
    def __init__(self) -> None:
        self.books: dict[str, dict[str, dict[float, float]]] = {}

    def apply(self, message: dict[str, Any]) -> set[str]:
        changed: set[str] = set()
        event = message.get("event_type") or message.get("type")
        if event == "book":
            asset = str(message.get("asset_id") or message.get("assetId") or "")
            if asset:
                self.books[asset] = {"bids": self._levels(message.get("bids")), "asks": self._levels(message.get("asks"))}
                changed.add(asset)
        elif event == "price_change":
            for change in message.get("price_changes", message.get("priceChanges", [])):
                asset = str(change.get("asset_id") or change.get("assetId") or message.get("asset_id") or "")
                side = "bids" if str(change.get("side", "")).lower() in {"buy", "bid"} else "asks" if str(change.get("side", "")).lower() in {"sell", "ask"} else ""
                if asset and side:
                    book = self.books.setdefault(asset, {"bids": {}, "asks": {}})
                    price, size = float(change["price"]), float(change["size"])
                    if size == 0: book[side].pop(price, None)
                    else: book[side][price] = size
                    changed.add(asset)
        return changed

    @staticmethod
    def _levels(value: Any) -> dict[float, float]:
        result = {}
        for row in value or []:
            try: result[float(row["price"])] = float(row["size"])
            except (KeyError, TypeError, ValueError): pass
        return result

    def top(self, asset: str) -> dict[str, float | None]:
        book = self.books.get(asset, {"bids": {}, "asks": {}})
        bids, asks = book["bids"], book["asks"]
        return {"bid": max(bids) if bids else None, "bid_size": bids[max(bids)] if bids else 0.0, "ask": min(asks) if asks else None, "ask_size": asks[min(asks)] if asks else 0.0}


@dataclass
class StreamStats:
    started_at_utc: str
    connection_count: int = 0
    reconnect_count: int = 0
    disconnect_count: int = 0
    protocol_error_count: int = 0
    error_count: int = 0
    messages: int = 0
    last_message_at_utc: str | None = None
    stale_stream_events: int = 0
    max_recovery_seconds: float | None = None
    last_disconnect_mono: float | None = None
    details: list[dict[str, Any]] = field(default_factory=list)

    def record(self, kind: str, detail: dict[str, Any] | None = None) -> None:
        if kind == "connection": self.connection_count += 1
        elif kind == "reconnect": self.reconnect_count += 1
        elif kind == "disconnect": self.disconnect_count += 1; self.last_disconnect_mono = time.monotonic()
        elif kind == "protocol_error": self.protocol_error_count += 1
        elif kind == "error": self.error_count += 1
        elif kind == "stale_stream": self.stale_stream_events += 1
        if kind == "connection" and self.last_disconnect_mono is not None:
            recovery = time.monotonic() - self.last_disconnect_mono
            self.max_recovery_seconds = max(self.max_recovery_seconds or 0.0, recovery)
        if len(self.details) < 100: self.details.append({"kind": kind, **(detail or {})})


async def consume_market_stream(
    assets: Iterable[str],
    on_message: Callable[[dict[str, Any], int], Awaitable[None]],
    *,
    stop: asyncio.Event,
    stats: StreamStats,
    startup_event: Callable[[str, str, dict[str, Any]], Awaitable[None]] | None = None,
    connect_timeout: float = 20.0,
    subscription_timeout: float = 10.0,
    first_message_timeout: float = 30.0,
    stale_seconds: float = 30.0,
) -> None:
    try:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, WebSocketException
    except ImportError as exc:
        raise RuntimeError("websockets==16.1.1 is required for live census streaming") from exc
    assets = [str(asset) for asset in assets]
    first = True
    startup_complete = False
    startup_phase = "websocket_connection"

    async def notify(phase: str, state: str, detail: dict[str, Any] | None = None) -> None:
        if startup_event is not None:
            await startup_event(phase, state, detail or {})

    while not stop.is_set():
        try:
            # The declared 100-event bootstrap can exceed the websockets default
            # 1 MiB frame limit. It is public data only; keep frame size unlimited
            # and persist bounded episode state rather than raw messages.
            if not startup_complete:
                startup_phase = "websocket_connection"
                await notify(startup_phase, "before", {"url": WS_URL, "timeout_seconds": connect_timeout})
            async with connect(WS_URL, ping_interval=None, open_timeout=connect_timeout, max_size=None) as socket:
                stats.record("connection")
                if not first: stats.record("reconnect")
                first = False
                if not startup_complete:
                    await notify(startup_phase, "after", {"connection_count": stats.connection_count})
                    startup_phase = "subscription_construction_send"
                    await notify(startup_phase, "before", {"asset_count": len(assets), "timeout_seconds": subscription_timeout})
                    subscription_deadline = time.monotonic() + subscription_timeout
                subscription = json.dumps({"assets_ids": assets, "type": "market", "custom_feature_enabled": True, "initial_dump": True})
                send_timeout = max(0.001, subscription_deadline - time.monotonic()) if not startup_complete else subscription_timeout
                await asyncio.wait_for(socket.send(subscription), timeout=send_timeout)
                if not startup_complete:
                    await notify(startup_phase, "after", {"asset_count": len(assets), "payload_bytes": len(subscription.encode("utf-8"))})
                    startup_phase = "first_message_receipt"
                    await notify(startup_phase, "before", {"timeout_seconds": first_message_timeout})
                    first_message_deadline = time.monotonic() + first_message_timeout
                last_ping = time.monotonic(); last_message = time.monotonic()
                while not stop.is_set():
                    now = time.monotonic()
                    if now - last_ping >= 10:
                        await socket.send("PING"); last_ping = now
                    if now - last_message >= stale_seconds:
                        stats.record("stale_stream", {"stale_seconds": now - last_message})
                        last_message = now
                    receive_timeout = 1.0
                    if not startup_complete:
                        remaining = first_message_deadline - now
                        if remaining <= 0:
                            raise StreamStartupError(f"first_message_receipt timed out after {first_message_timeout:.1f}s")
                        receive_timeout = min(receive_timeout, remaining)
                    try:
                        raw = await asyncio.wait_for(socket.recv(), timeout=receive_timeout)
                    except TimeoutError:
                        if not startup_complete and time.monotonic() >= first_message_deadline:
                            raise StreamStartupError(f"first_message_receipt timed out after {first_message_timeout:.1f}s")
                        continue
                    if raw in {"PONG", "pong"}: continue
                    try:
                        payload = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        stats.record("protocol_error", {"error": f"{type(exc).__name__}: {exc}"})
                        continue
                    messages = [message for message in (payload if isinstance(payload, list) else [payload]) if isinstance(message, dict)]
                    if not messages:
                        continue
                    last_message = time.monotonic(); stats.messages += 1; stats.last_message_at_utc = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z")
                    if not startup_complete:
                        startup_complete = True
                        await notify(startup_phase, "after", {"messages": stats.messages, "last_message_at_utc": stats.last_message_at_utc})
                    for message in messages:
                        if isinstance(message, dict): await on_message(message, time.monotonic_ns())
        except StreamStartupError as exc:
            await notify(startup_phase, "failed", {"error": f"{type(exc).__name__}: {exc}"})
            raise
        except (ConnectionClosed, WebSocketException, OSError, TimeoutError) as exc:
            stats.record("disconnect", {"error": f"{type(exc).__name__}: {exc}"})
            if not startup_complete:
                await notify(startup_phase, "failed", {"error": f"{type(exc).__name__}: {exc}"})
                raise StreamStartupError(f"{startup_phase} failed: {type(exc).__name__}: {exc}") from exc
            if not stop.is_set(): await asyncio.sleep(2.0)
        except Exception as exc:
            stats.record("error", {"error": f"{type(exc).__name__}: {exc}"})
            if not startup_complete:
                await notify(startup_phase, "failed", {"error": f"{type(exc).__name__}: {exc}"})
            raise
