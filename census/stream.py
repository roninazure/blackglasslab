from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


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


async def consume_market_stream(assets: Iterable[str], on_message: Callable[[dict[str, Any], int], Awaitable[None]], *, stop: asyncio.Event, stats: StreamStats, stale_seconds: float = 30.0) -> None:
    try:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, WebSocketException
    except ImportError as exc:
        raise RuntimeError("websockets==16.1.1 is required for live census streaming") from exc
    assets = [str(asset) for asset in assets]
    first = True
    while not stop.is_set():
        connected_at = time.monotonic()
        try:
            # The declared 100-event bootstrap can exceed the websockets default
            # 1 MiB frame limit. It is public data only; keep frame size unlimited
            # and persist bounded episode state rather than raw messages.
            async with connect(WS_URL, ping_interval=None, open_timeout=20, max_size=None) as socket:
                stats.record("connection")
                if not first: stats.record("reconnect")
                first = False
                await socket.send(json.dumps({"assets_ids": assets, "type": "market", "custom_feature_enabled": True, "initial_dump": True}))
                last_ping = time.monotonic(); last_message = time.monotonic()
                while not stop.is_set():
                    now = time.monotonic()
                    if now - last_ping >= 10:
                        await socket.send("PING"); last_ping = now
                    if now - last_message >= stale_seconds:
                        stats.record("stale_stream", {"stale_seconds": now - last_message})
                        last_message = now
                    try:
                        raw = await asyncio.wait_for(socket.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    if raw in {"PONG", "pong"}: continue
                    try:
                        payload = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        stats.record("protocol_error", {"error": f"{type(exc).__name__}: {exc}"})
                        continue
                    last_message = time.monotonic(); stats.messages += 1; stats.last_message_at_utc = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z")
                    for message in payload if isinstance(payload, list) else [payload]:
                        if isinstance(message, dict): await on_message(message, time.monotonic_ns())
        except (ConnectionClosed, WebSocketException, OSError, asyncio.TimeoutError) as exc:
            stats.record("disconnect", {"error": f"{type(exc).__name__}: {exc}"})
            if not stop.is_set(): await asyncio.sleep(2.0)
        except Exception as exc:
            stats.record("error", {"error": f"{type(exc).__name__}: {exc}"})
            raise
