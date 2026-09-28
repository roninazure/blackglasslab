import multiprocessing
import os
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from parallax.pmus_acquisition import (
    BOOK_REQUESTS_PER_MINUTE,
    PMUSAcquisition,
    PMUSAcquisitionUnavailable,
    PMUSCircuitOpen,
    PRIORITY_CONFIRMATION,
    PRIORITY_DISCOVERY,
    REQUEST_DEADLINE_SECONDS,
    RESERVED_CONFIRMATION_BOOKS,
    TOTAL_REQUESTS_PER_MINUTE,
)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 28, 12, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def book(slug, *, yes=0.50, no=0.50):
    return {
        f"{slug}::YES": {
            "best_bid": yes - 0.02,
            "best_ask": yes,
            "bid_size_shares": 100,
            "ask_size_shares": 100,
        },
        f"{slug}::NO": {
            "best_bid": no - 0.02,
            "best_ask": no,
            "bid_size_shares": 100,
            "ask_size_shares": 100,
        },
    }


def policy(path, lane, clock):
    return PMUSAcquisition(
        lane,
        state_path=path,
        clock=clock,
        sleeper=clock.advance,
    )


def _subprocess_acquire(
    state_path,
    kind,
    key,
    outcome,
    ready,
    start,
    results,
):
    acquisition = PMUSAcquisition("SUBPROCESS", state_path=state_path)
    provider_calls = 0

    def request():
        nonlocal provider_calls
        provider_calls += 1
        time.sleep(0.25)
        if outcome == "error":
            raise RuntimeError("local provider failure")
        if kind == "discovery":
            return [{"slug": key}]
        return book(key)

    ready.put(True)
    start.wait(5)
    try:
        if kind == "discovery":
            value = acquisition.discover(key, request)
            source = value[0]["slug"]
        else:
            value = acquisition.book(key, request, fair_probability=0.70)
            source = value.source
        results.put(
            {
                "ok": True,
                "provider_calls": provider_calls,
                "source": source,
            }
        )
    except Exception as exc:  # The parent asserts the exact shared outcome.
        results.put(
            {
                "ok": False,
                "provider_calls": provider_calls,
                "error": type(exc).__name__,
            }
        )


def _subprocess_die_holding_lock(state_path, key, ready):
    acquisition = PMUSAcquisition("SUBPROCESS", state_path=state_path)
    with acquisition._singleflight(key):
        ready.set()
        os._exit(17)


def _subprocess_hold_lock(state_path, key, ready, release):
    acquisition = PMUSAcquisition("SUBPROCESS", state_path=state_path)
    with acquisition._singleflight(key):
        ready.set()
        release.wait(10)


def _run_competing_subprocesses(state_path, kind, key, outcome="ok"):
    PMUSAcquisition("INITIALIZE", state_path=state_path)
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_subprocess_acquire,
            args=(state_path, kind, key, outcome, ready, start, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for _ in processes:
        assert ready.get(timeout=10) is True
    start.set()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    return [results.get(timeout=5) for _ in processes]


def _request_log(state_path):
    with sqlite3.connect(state_path) as connection:
        return connection.execute(
            "SELECT requested_at, kind, priority FROM request_log ORDER BY requested_at"
        ).fetchall()


def _assert_sliding_limit(timestamps, limit):
    for timestamp in timestamps:
        assert (
            sum(timestamp - 60.0 < prior <= timestamp for prior in timestamps)
            <= limit
        )


def test_cross_cycle_fresh_book_cache_reuse(tmp_path):
    state = tmp_path / "pmus.sqlite"
    clock = Clock()
    calls = []
    slug = "nfl-cin-pit-2026-09-28"

    first = policy(state, "NFL", clock).book(
        slug,
        lambda: calls.append(slug) or book(slug),
        fair_probability=0.70,
        calibration_edge=0.075465,
    )
    clock.advance(30)
    second_policy = policy(state, "NFL", clock)
    second = second_policy.book(
        slug,
        lambda: calls.append("unexpected") or book(slug),
        fair_probability=0.70,
        calibration_edge=0.075465,
    )

    assert first.source == "PROVIDER"
    assert second.source == "FRESH_CACHE"
    assert first.observed_at == second.observed_at
    assert calls == [slug]
    assert second_policy.diagnostics()["cache_hits"] == 1


def test_decision_relevant_stale_book_requires_fresh_provider_evidence(tmp_path):
    state = tmp_path / "pmus.sqlite"
    clock = Clock()
    calls = []
    slug = "aec-mlb-laa-wsh-2026-09-28"
    first_policy = policy(state, "MLB", clock)
    first_policy.book(
        slug,
        lambda: calls.append("first") or book(slug, yes=0.45, no=0.55),
        fair_probability=0.62,
    )

    clock.advance(61)
    refreshed = policy(state, "MLB", clock).book(
        slug,
        lambda: calls.append("refresh") or book(slug, yes=0.44, no=0.56),
        fair_probability=0.62,
    )

    assert refreshed.source == "PROVIDER"
    assert refreshed.requested is True
    assert calls == ["first", "refresh"]


def test_buy_confirmation_never_falls_back_to_stale_book(tmp_path):
    state = tmp_path / "pmus.sqlite"
    clock = Clock()
    slug = "nfl-bal-kc-2026-09-28"
    policy(state, "NFL", clock).book(
        slug,
        lambda: book(slug, yes=0.40, no=0.60),
        fair_probability=0.65,
        calibration_edge=0.075465,
    )
    clock.advance(61)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        policy(state, "NFL", clock).book(
            slug,
            lambda: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
            fair_probability=0.65,
            calibration_edge=0.075465,
        )


def test_obvious_noncandidate_uses_longer_reference_ttl_without_book_call(tmp_path):
    state = tmp_path / "pmus.sqlite"
    clock = Clock()
    slug = "aec-mlb-laa-wsh-2026-09-28"
    calls = []
    policy(state, "MLB", clock).book(
        slug,
        lambda: calls.append("first") or book(slug, yes=0.56, no=0.56),
        fair_probability=0.50,
    )
    clock.advance(61)
    second_policy = policy(state, "MLB", clock)
    result = second_policy.book(
        slug,
        lambda: calls.append("unexpected") or book(slug),
        fair_probability=0.50,
    )

    assert result.book is None
    assert result.source == "NONCANDIDATE_CACHE"
    assert calls == ["first"]
    assert second_policy.diagnostics()["requests_avoided"] == 1


def test_rate_limit_opens_shared_circuit_and_blocks_later_provider_call(tmp_path):
    class RateLimit(Exception):
        status_code = 429
        body = "Cloudflare 1015"

    state = tmp_path / "pmus.sqlite"
    clock = Clock()
    first = policy(state, "NFL", clock)
    with pytest.raises(PMUSCircuitOpen):
        first.book(
            "nfl-one",
            lambda: (_ for _ in ()).throw(RateLimit("limited")),
            fair_probability=0.70,
            calibration_edge=0.075465,
        )

    later_calls = []
    second = policy(state, "MLB", clock)
    with pytest.raises(PMUSCircuitOpen):
        second.book(
            "mlb-two",
            lambda: later_calls.append(1) or book("mlb-two"),
            fair_probability=0.70,
        )

    assert later_calls == []
    assert second.diagnostics()["circuit_open"] is True


def test_global_governor_stays_below_book_safety_budget(tmp_path):
    state = tmp_path / "pmus.sqlite"
    clock = Clock()
    calls = []
    results = []
    for lane in ("NFL", "MLB"):
        acquisition = policy(state, lane, clock)
        for index in range(4):
            slug = f"{lane.lower()}-{index}"
            result = acquisition.book(
                slug,
                lambda slug=slug: calls.append(slug) or book(slug),
                fair_probability=0.70,
            )
            results.append(result)

    blocked = policy(state, "NFL-SECOND", clock).book(
        "nfl-ninth",
        lambda: calls.append("ninth") or book("nfl-ninth"),
        fair_probability=0.70,
    )

    diagnostics = policy(state, "TEST", clock).diagnostics()
    assert len(calls) == BOOK_REQUESTS_PER_MINUTE - RESERVED_CONFIRMATION_BOOKS
    assert sum(result.book is None for result in results) == RESERVED_CONFIRMATION_BOOKS
    assert blocked.book is None
    assert diagnostics["book_requests_last_minute"] <= BOOK_REQUESTS_PER_MINUTE


def test_targeted_discovery_is_reused_without_second_request(tmp_path):
    state = tmp_path / "pmus.sqlite"
    clock = Clock()
    calls = []
    rows = [{"slug": "nfl-one"}]

    first = policy(state, "NFL", clock).discover(
        "NFL:slate", lambda: calls.append(1) or rows
    )
    clock.advance(60)
    second_policy = policy(state, "MLB", clock)
    second = second_policy.discover(
        "NFL:slate", lambda: calls.append(2) or []
    )

    assert first == second == rows
    assert calls == [1]
    assert second_policy.diagnostics()["discovery_requests"] == 0


def test_confirmation_uses_reserved_token_immediately_after_ordinary_request(tmp_path):
    state = tmp_path / "pmus.sqlite"
    clock = Clock()
    acquisition = policy(state, "NFL", clock)

    acquisition._reserve(
        "discovery",
        priority=PRIORITY_DISCOVERY,
        deadline_seconds=REQUEST_DEADLINE_SECONDS,
    )
    ordinary_at = clock().timestamp()
    acquisition._reserve(
        "book",
        priority=PRIORITY_CONFIRMATION,
        deadline_seconds=REQUEST_DEADLINE_SECONDS,
    )

    assert clock().timestamp() == ordinary_at


def test_two_ordinary_requests_retain_six_second_pacing(tmp_path):
    state = tmp_path / "pmus.sqlite"
    clock = Clock()
    acquisition = policy(state, "NFL", clock)

    acquisition._reserve(
        "discovery",
        priority=PRIORITY_DISCOVERY,
        deadline_seconds=REQUEST_DEADLINE_SECONDS,
    )
    first_at = clock().timestamp()
    acquisition._reserve(
        "discovery",
        priority=PRIORITY_DISCOVERY,
        deadline_seconds=REQUEST_DEADLINE_SECONDS,
    )

    assert clock().timestamp() - first_at == pytest.approx(6.0)


def test_reserved_token_bounds_immediate_burst_to_two_requests(tmp_path):
    state = tmp_path / "pmus.sqlite"
    clock = Clock()
    acquisition = policy(state, "NFL", clock)

    acquisition._reserve(
        "discovery",
        priority=PRIORITY_DISCOVERY,
        deadline_seconds=REQUEST_DEADLINE_SECONDS,
    )
    acquisition._reserve(
        "book",
        priority=PRIORITY_CONFIRMATION,
        deadline_seconds=REQUEST_DEADLINE_SECONDS,
    )
    acquisition._reserve(
        "book",
        priority=PRIORITY_CONFIRMATION,
        deadline_seconds=REQUEST_DEADLINE_SECONDS,
    )

    timestamps = [row[0] for row in _request_log(state)]
    assert timestamps[0] == timestamps[1]
    assert timestamps[2] - timestamps[1] == pytest.approx(6.0)


def test_rolling_total_window_never_exceeds_hard_ceiling(tmp_path):
    state = tmp_path / "pmus.sqlite"
    clock = Clock()
    acquisition = policy(state, "NFL", clock)
    admitted_at = []

    for _ in range(15):
        acquisition._reserve(
            "discovery",
            priority=PRIORITY_CONFIRMATION,
            deadline_seconds=61.0,
        )
        admitted_at.append(clock().timestamp())

    _assert_sliding_limit(admitted_at, TOTAL_REQUESTS_PER_MINUTE)


def test_rolling_book_window_never_exceeds_hard_ceiling(tmp_path):
    state = tmp_path / "pmus.sqlite"
    clock = Clock()
    acquisition = policy(state, "NFL", clock)
    admitted_at = []

    for _ in range(12):
        acquisition._reserve(
            "book",
            priority=PRIORITY_CONFIRMATION,
            deadline_seconds=61.0,
        )
        admitted_at.append(clock().timestamp())

    _assert_sliding_limit(admitted_at, BOOK_REQUESTS_PER_MINUTE)


def test_ordinary_traffic_cannot_consume_global_confirmation_slots(tmp_path):
    state = tmp_path / "pmus.sqlite"
    clock = Clock()
    acquisition = policy(state, "NFL", clock)

    for _ in range(TOTAL_REQUESTS_PER_MINUTE - RESERVED_CONFIRMATION_BOOKS):
        acquisition._reserve(
            "discovery",
            priority=PRIORITY_DISCOVERY,
            deadline_seconds=REQUEST_DEADLINE_SECONDS,
        )
    with pytest.raises(PMUSAcquisitionUnavailable, match="total capacity reserved"):
        acquisition._reserve(
            "discovery",
            priority=PRIORITY_DISCOVERY,
            deadline_seconds=REQUEST_DEADLINE_SECONDS,
        )

    for _ in range(RESERVED_CONFIRMATION_BOOKS):
        acquisition._reserve(
            "book",
            priority=PRIORITY_CONFIRMATION,
            deadline_seconds=REQUEST_DEADLINE_SECONDS,
        )

    assert len(_request_log(state)) == TOTAL_REQUESTS_PER_MINUTE


def test_ordinary_books_cannot_consume_reserved_book_slots(tmp_path):
    state = tmp_path / "pmus.sqlite"
    clock = Clock()
    acquisition = policy(state, "NFL", clock)

    for _ in range(BOOK_REQUESTS_PER_MINUTE - RESERVED_CONFIRMATION_BOOKS):
        acquisition._reserve(
            "book",
            priority=PRIORITY_DISCOVERY,
            deadline_seconds=REQUEST_DEADLINE_SECONDS,
        )
    with pytest.raises(PMUSAcquisitionUnavailable, match="book capacity reserved"):
        acquisition._reserve(
            "book",
            priority=PRIORITY_DISCOVERY,
            deadline_seconds=REQUEST_DEADLINE_SECONDS,
        )

    for _ in range(RESERVED_CONFIRMATION_BOOKS):
        acquisition._reserve(
            "book",
            priority=PRIORITY_CONFIRMATION,
            deadline_seconds=REQUEST_DEADLINE_SECONDS,
        )

    assert len(_request_log(state)) == BOOK_REQUESTS_PER_MINUTE


def test_confirmation_cannot_lose_reserved_token_to_waiting_ordinary_work(tmp_path):
    state = tmp_path / "pmus.sqlite"
    clock = Clock()
    seed = policy(state, "NFL", clock)
    seed._reserve(
        "discovery",
        priority=PRIORITY_DISCOVERY,
        deadline_seconds=REQUEST_DEADLINE_SECONDS,
    )
    ordinary_waiting = threading.Event()
    release_ordinary = threading.Event()
    ordinary_error = []

    def blocked_sleep(seconds):
        ordinary_waiting.set()
        assert release_ordinary.wait(5)
        clock.advance(seconds)

    ordinary = PMUSAcquisition(
        "MLB",
        state_path=state,
        clock=clock,
        sleeper=blocked_sleep,
    )

    def reserve_ordinary():
        try:
            ordinary._reserve(
                "discovery",
                priority=PRIORITY_DISCOVERY,
                deadline_seconds=REQUEST_DEADLINE_SECONDS,
            )
        except Exception as exc:
            ordinary_error.append(exc)

    waiter = threading.Thread(target=reserve_ordinary)
    waiter.start()
    assert ordinary_waiting.wait(5)
    seed._reserve(
        "book",
        priority=PRIORITY_CONFIRMATION,
        deadline_seconds=REQUEST_DEADLINE_SECONDS,
    )
    release_ordinary.set()
    waiter.join(5)

    assert not waiter.is_alive()
    assert len(ordinary_error) == 1
    assert isinstance(ordinary_error[0], PMUSAcquisitionUnavailable)
    assert [row[2] for row in _request_log(state)] == [
        PRIORITY_DISCOVERY,
        PRIORITY_CONFIRMATION,
    ]


def test_cross_process_discovery_singleflight_uses_one_provider_reservation(tmp_path):
    state = tmp_path / "pmus.sqlite"
    results = _run_competing_subprocesses(state, "discovery", "NFL:shared")

    assert sum(result["provider_calls"] for result in results) == 1
    assert all(result["ok"] for result in results)
    assert len(_request_log(state)) == 1


def test_cross_process_book_singleflight_reuses_successful_owner_cache(tmp_path):
    state = tmp_path / "pmus.sqlite"
    results = _run_competing_subprocesses(state, "book", "nfl-shared")

    assert sum(result["provider_calls"] for result in results) == 1
    assert all(result["ok"] for result in results)
    assert {result["source"] for result in results} == {"PROVIDER", "FRESH_CACHE"}
    assert len(_request_log(state)) == 1


def test_cross_process_negative_cache_prevents_duplicate_provider_call(tmp_path):
    state = tmp_path / "pmus.sqlite"
    results = _run_competing_subprocesses(
        state, "discovery", "NFL:negative", outcome="error"
    )

    assert sum(result["provider_calls"] for result in results) == 1
    assert not any(result["ok"] for result in results)
    assert {result["error"] for result in results} == {
        "RuntimeError",
        "PMUSAcquisitionUnavailable",
    }
    assert len(_request_log(state)) == 1


def test_process_death_releases_singleflight_lock_for_follower(tmp_path):
    state = tmp_path / "pmus.sqlite"
    PMUSAcquisition("INITIALIZE", state_path=state)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(
        target=_subprocess_die_holding_lock,
        args=(state, "discovery:NFL:death", ready),
    )
    process.start()
    assert ready.wait(10)
    process.join(10)
    assert process.exitcode == 17

    calls = []
    rows = PMUSAcquisition("NFL", state_path=state).discover(
        "NFL:death", lambda: calls.append(1) or [{"slug": "recovered"}]
    )

    assert rows == [{"slug": "recovered"}]
    assert calls == [1]


def test_live_owner_past_deadline_fails_closed_and_different_key_is_free(tmp_path):
    state = tmp_path / "pmus.sqlite"
    PMUSAcquisition("INITIALIZE", state_path=state)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_subprocess_hold_lock,
        args=(state, "discovery:NFL:held", ready, release),
    )
    process.start()
    assert ready.wait(10)

    clock = Clock()
    follower = policy(state, "NFL", clock)
    with follower._singleflight("discovery:NFL:different"):
        pass
    calls = []
    with pytest.raises(PMUSAcquisitionUnavailable, match="lock deferred"):
        follower.discover(
            "NFL:held", lambda: calls.append(1) or [{"slug": "unexpected"}]
        )

    release.set()
    process.join(10)
    assert process.exitcode == 0
    assert calls == []
    assert clock.now == datetime(2026, 9, 28, 12, tzinfo=UTC) + timedelta(
        seconds=REQUEST_DEADLINE_SECONDS
    )
