import asyncio
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from parallax.pmus_market_data import (
    PMUSMarketDataState,
    PMUSMarketDataStream,
    PMUSMarketDataUnavailable,
)


def message(slug, *, bid="0.49", ask="0.51"):
    return {
        "marketData": {
            "marketSlug": slug,
            "bids": [{"px": {"value": bid, "currency": "USD"}, "qty": "10"}],
            "offers": [{"px": {"value": ask, "currency": "USD"}, "qty": "12"}],
            "state": "MARKET_STATE_OPEN",
            "transactTime": "2026-09-28T12:00:00Z",
            "stats": {},
        }
    }


class FakeWebsocket:
    def __init__(self, action=None):
        self.handlers = {}
        self.action = action
        self.subscriptions = []
        self.connect_calls = 0
        self.close_calls = 0

    def on(self, event, callback):
        self.handlers[event] = callback

    async def connect(self):
        self.connect_calls += 1

    async def subscribe_market_data(self, subscription_id, slugs):
        self.subscriptions.append((subscription_id, list(slugs)))
        if self.action is not None:
            self.action(self)

    async def close(self):
        self.close_calls += 1


class FakeClient:
    def __init__(self, websockets):
        self.websockets = list(websockets)
        self.created = []
        self.ws = SimpleNamespace(markets=self.markets)

    def markets(self):
        websocket = self.websockets.pop(0)
        self.created.append(websocket)
        return websocket


def emit_books(*slugs):
    def action(websocket):
        for slug in slugs:
            websocket.handlers["market_data"](message(slug))

    return action


def disconnect(websocket):
    websocket.handlers["close"]()


def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_multi_slug_subscription_readiness_normalization_and_observed_timestamp():
    slugs = ["nfl-bal-kc-2026-09-28", "nfl-cin-pit-2026-09-28"]
    websocket = FakeWebsocket(emit_books(*slugs))
    observed = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    stream = PMUSMarketDataStream(
        FakeClient([websocket]),
        slugs,
        stale_seconds=10,
        utcnow=lambda: observed,
    )

    stream.start()
    assert stream.wait_ready(1)
    snapshot = stream.book(slugs[0])

    assert websocket.subscriptions == [("parallax-sports-books", sorted(slugs))]
    assert snapshot.observed_at == observed.isoformat()
    assert snapshot.book[f"{slugs[0]}::YES"]["best_bid"] == 0.49
    assert snapshot.book[f"{slugs[0]}::YES"]["best_ask"] == 0.51
    assert snapshot.book[f"{slugs[0]}::NO"]["best_bid"] == 0.49
    assert snapshot.book[f"{slugs[0]}::NO"]["best_ask"] == 0.51
    assert stream.diagnostics()["readiness"] == "FULL"
    assert stream.stop() is True
    assert websocket.close_calls == 1
    assert stream.state == PMUSMarketDataState.STOPPED
    with pytest.raises(PMUSMarketDataUnavailable):
        stream.book(slugs[0])


def test_missing_book_fails_closed():
    present = "nfl-bal-kc-2026-09-28"
    missing = "nfl-cin-pit-2026-09-28"
    websocket = FakeWebsocket(emit_books(present))
    stream = PMUSMarketDataStream(
        FakeClient([websocket]), [present, missing], stale_seconds=10
    )
    stream.start()
    assert wait_until(lambda: stream.state == PMUSMarketDataState.HEALTHY)
    assert stream.wait_ready(0.02) is False
    assert stream.diagnostics()["readiness"] == "PARTIAL"

    with pytest.raises(PMUSMarketDataUnavailable, match="missing streamed"):
        stream.book(missing)

    websocket.handlers["market_data"](message(missing))
    assert stream.wait_ready(1) is True
    stream.stop()


def test_connection_cycle_honors_stop_before_websocket_construction():
    class NoConnectionClient:
        ws = SimpleNamespace(
            markets=lambda: (_ for _ in ()).throw(
                AssertionError("stopped cycle must not construct a websocket")
            )
        )

    stream = PMUSMarketDataStream(NoConnectionClient(), ["nfl-bal-kc-2026-09-28"])
    stream._stop_thread.set()

    assert asyncio.run(stream._connection_cycle()) == (None, False)


def test_invalid_bid_ask_fails_stream_closed():
    slug = "nfl-bal-kc-2026-09-28"
    websocket = FakeWebsocket(
        lambda ws: ws.handlers["market_data"](message(slug, bid="0.60", ask="0.50"))
    )
    stream = PMUSMarketDataStream(FakeClient([websocket]), [slug])

    stream.start()
    assert wait_until(lambda: stream.failed)
    assert stream.wait_ready(0.1) is False
    with pytest.raises(PMUSMarketDataUnavailable):
        stream.book(slug)
    stream.stop()


def test_unexpected_slug_fails_stream_closed():
    expected = "nfl-bal-kc-2026-09-28"
    websocket = FakeWebsocket(
        lambda ws: ws.handlers["market_data"](message("nfl-wrong-market"))
    )
    stream = PMUSMarketDataStream(FakeClient([websocket]), [expected])

    stream.start()
    assert wait_until(lambda: stream.failed)
    assert "unexpected" in (stream.error or "").lower()
    stream.stop()


def test_stale_book_fails_closed():
    slug = "nfl-bal-kc-2026-09-28"
    now = [100.0]
    websocket = FakeWebsocket(emit_books(slug))
    stream = PMUSMarketDataStream(
        FakeClient([websocket]),
        [slug],
        stale_seconds=5,
        monotonic=lambda: now[0],
    )
    stream.start()
    assert stream.wait_ready(1)

    now[0] += 6
    assert stream.ready() is False
    with pytest.raises(PMUSMarketDataUnavailable, match="stale"):
        stream.book(slug)
    stream.stop()


def test_disconnect_reconnects_and_resubscribes():
    slug = "nfl-bal-kc-2026-09-28"
    first = FakeWebsocket(disconnect)
    second = FakeWebsocket(emit_books(slug))
    stream = PMUSMarketDataStream(
        FakeClient([first, second]),
        [slug],
        backoff_initial_seconds=0,
        backoff_cap_seconds=0,
    )

    stream.start()
    assert stream.wait_ready(1)
    assert (
        first.subscriptions
        == second.subscriptions
        == [("parallax-sports-books", [slug])]
    )
    assert stream.diagnostics()["reconnects"] == 1
    first.handlers["market_data"](message("nfl-unsubscribed-late-message"))
    assert stream.failed is False
    stream.stop()


def test_reconnect_backoff_is_exponential_and_bounded():
    slug = "nfl-bal-kc-2026-09-28"
    waits = []

    def waiter(_event, delay):
        waits.append(delay)
        return False

    client = FakeClient([FakeWebsocket(disconnect) for _ in range(3)])
    stream = PMUSMarketDataStream(
        client,
        [slug],
        max_reconnect_attempts=2,
        backoff_initial_seconds=2,
        backoff_cap_seconds=3,
        waiter=waiter,
    )

    stream.start()
    assert wait_until(lambda: stream.failed)
    assert waits == [2, 3]
    assert len(client.created) == 3
    assert stream.diagnostics()["backoff_seconds"] == [2, 3]
    stream.stop()


def test_stop_interrupts_recovery_wait():
    slug = "nfl-bal-kc-2026-09-28"
    waiting = threading.Event()

    def waiter(event, _delay):
        waiting.set()
        return event.wait(30)

    stream = PMUSMarketDataStream(
        FakeClient([FakeWebsocket(disconnect)]),
        [slug],
        backoff_initial_seconds=30,
        backoff_cap_seconds=30,
        waiter=waiter,
    )
    stream.start()
    assert waiting.wait(1)

    started = time.monotonic()
    assert stream.stop() is True
    assert time.monotonic() - started < 1


def test_no_client_and_no_credentials_never_start_rest_polling():
    slug = "nfl-bal-kc-2026-09-28"
    stream = PMUSMarketDataStream(None, [slug])
    stream.start()

    assert stream.failed
    assert stream.diagnostics()["rest_polling"] is False
    assert stream.stop() is True

    constructed = []
    with pytest.raises(PMUSMarketDataUnavailable, match="missing"):
        PMUSMarketDataStream.from_env(
            [slug],
            environ={},
            client_factory=lambda **kwargs: constructed.append(kwargs),
        )
    assert constructed == []


def test_client_construction_failure_does_not_leak_credentials():
    secret = "test-secret-must-not-appear"

    def fail_factory(**_kwargs):
        raise RuntimeError(f"failed with {secret}")

    with pytest.raises(PMUSMarketDataUnavailable) as caught:
        PMUSMarketDataStream.from_env(
            ["nfl-bal-kc-2026-09-28"],
            environ={
                "PARALLAX_PMUS_KEY_ID": "test-key-must-not-appear",
                "PARALLAX_PMUS_SECRET_KEY": secret,
            },
            client_factory=fail_factory,
        )

    assert secret not in str(caught.value)
    assert "test-key-must-not-appear" not in str(caught.value)


def test_book_snapshot_isolated_from_cache_mutation():
    slug = "nfl-bal-kc-2026-09-28"
    websocket = FakeWebsocket(emit_books(slug))
    stream = PMUSMarketDataStream(FakeClient([websocket]), [slug])
    stream.start()
    assert stream.wait_ready(1)

    first = stream.book(slug)
    first.book[f"{slug}::YES"]["best_bid"] = 0.01
    assert stream.book(slug).book[f"{slug}::YES"]["best_bid"] == 0.49
    stream.stop()

