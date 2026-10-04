from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

from parallax.nfl_runtime_state import (
    KALSHI_DISCOVERY_INTERVAL_SECONDS,
    KALSHI_REFRESH_MAX_MARKETS,
    PMUS_STARTUP_COOLDOWN_SECONDS,
    NFLRuntimeState,
)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def test_pmus_failure_persists_across_instances_and_expires(tmp_path):
    clock = [NOW]
    first = NFLRuntimeState(tmp_path, clock=lambda: clock[0])
    opened = first.record_pmus_failure(
        classification="HTTP401", reason="authenticated stream rejected"
    )

    assert opened["state"] == "COOLDOWN"
    assert opened["attempt_allowed"] is False
    assert opened["seconds_remaining"] == PMUS_STARTUP_COOLDOWN_SECONDS

    restarted = NFLRuntimeState(tmp_path, clock=lambda: clock[0])
    assert restarted.pmus_status()["attempt_allowed"] is False

    clock[0] += timedelta(seconds=PMUS_STARTUP_COOLDOWN_SECONDS - 1)
    assert restarted.pmus_status()["attempt_allowed"] is False
    clock[0] += timedelta(seconds=1)
    assert restarted.pmus_status()["attempt_allowed"] is True


def test_corrupt_pmus_state_fails_closed_for_bounded_window_then_recovers(tmp_path):
    path = tmp_path / "nfl_pmus_startup_cooldown.json"
    path.write_text("{not-json", encoding="utf-8")
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    clock = [NOW]
    state = NFLRuntimeState(tmp_path, clock=lambda: clock[0])

    blocked = state.pmus_status()
    assert blocked["state"] == "CORRUPT_COOLDOWN"
    assert blocked["attempt_allowed"] is False
    assert blocked["failure_classification"] == "CORRUPT_STATE"

    clock[0] += timedelta(seconds=PMUS_STARTUP_COOLDOWN_SECONDS)
    assert state.pmus_status()["attempt_allowed"] is True


def test_kalshi_discovery_cache_has_hourly_boundary_and_no_quotes(tmp_path):
    clock = [NOW]
    state = NFLRuntimeState(tmp_path, clock=lambda: clock[0])
    rows = [{"ticker": "KXNFLGAME-A", "scheduled_start": "2026-10-05T17:00:00Z"}]
    state.record_kalshi_discovery(
        rows,
        actionable_tickers={"KXNFLGAME-A"},
        refreshed_at=clock[0],
    )

    persisted = json.loads(state.kalshi_path.read_text(encoding="utf-8"))
    assert "book" not in persisted
    assert "price" not in persisted
    assert state.kalshi_snapshot()["discovery_due"] is False

    clock[0] += timedelta(seconds=KALSHI_DISCOVERY_INTERVAL_SECONDS - 1)
    assert state.kalshi_snapshot()["discovery_due"] is False
    clock[0] += timedelta(seconds=1)
    assert state.kalshi_snapshot()["discovery_due"] is True


def test_kalshi_refresh_is_bounded_and_corrupt_cache_forces_discovery(tmp_path):
    state = NFLRuntimeState(tmp_path, clock=lambda: NOW)
    rows = [
        {
            "ticker": f"KXNFLGAME-{index:02d}",
            "scheduled_start": f"2026-10-05T{index:02d}:00:00Z",
        }
        for index in range(KALSHI_REFRESH_MAX_MARKETS + 1)
    ]
    state.record_kalshi_discovery(
        rows,
        actionable_tickers=set(),
        refreshed_at=NOW,
    )

    selected, coverage = state.kalshi_refresh_rows(state.kalshi_snapshot())
    assert len(selected) == KALSHI_REFRESH_MAX_MARKETS
    assert coverage["continuous_coverage"] is False
    assert coverage["unrefreshed_market_count"] == 1
    assert coverage["known_market_count"] == KALSHI_REFRESH_MAX_MARKETS + 1

    state.record_kalshi_refresh(
        NOW,
        observed_tickers={row["ticker"] for row in selected},
        actionable_tickers=set(),
        next_buy_cursor=coverage["next_buy_cursor"],
        next_other_cursor=coverage["next_other_cursor"],
        next_warm_cursor=coverage["next_warm_cursor"],
    )
    second, _coverage = state.kalshi_refresh_rows(state.kalshi_snapshot())
    assert coverage["unrefreshed_tickers"][0] in {row["ticker"] for row in second}

    state.kalshi_path.write_text("[]", encoding="utf-8")
    corrupt = state.kalshi_snapshot()
    assert corrupt["state"] == "CORRUPT"
    assert corrupt["discovery_due"] is True


def test_overflow_prioritizes_buys_but_rotates_watch_and_pass(tmp_path):
    state = NFLRuntimeState(tmp_path, clock=lambda: NOW)
    rows = [
        {"ticker": f"KXNFLGAME-{index:02d}", "scheduled_start": "2026-10-04T17:00:00Z"}
        for index in range(15)
    ]
    state.record_kalshi_discovery(
        rows,
        actionable_tickers={row["ticker"] for row in rows[:11]},
        refreshed_at=NOW,
    )

    seen_nonbuys = set()
    for _ in range(4):
        selected, coverage = state.kalshi_refresh_rows(state.kalshi_snapshot())
        selected_tickers = {row["ticker"] for row in selected}
        assert len(selected) == 12
        assert {row["ticker"] for row in rows[:11]} <= selected_tickers
        assert coverage["priority_buy_count"] == 11
        assert coverage["priority_buy_selected"] == 11
        assert coverage["unrefreshed_market_count"] == 3
        seen_nonbuys.update(selected_tickers - {row["ticker"] for row in rows[:11]})
        state.record_kalshi_refresh(
            NOW,
            observed_tickers=set(),
            actionable_tickers=set(),
            next_buy_cursor=coverage["next_buy_cursor"],
            next_other_cursor=coverage["next_other_cursor"],
            next_warm_cursor=coverage["next_warm_cursor"],
        )
    assert seen_nonbuys == {row["ticker"] for row in rows[11:]}


def test_legacy_buy_only_cache_forces_new_full_discovery(tmp_path):
    state = NFLRuntimeState(tmp_path, clock=lambda: NOW)
    state.kalshi_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "discovered_at": NOW.isoformat(),
                "markets": [{"ticker": "WATCH"}, {"ticker": "BUY"}],
                "refresh_tickers": ["BUY"],
            }
        ),
        encoding="utf-8",
    )
    snapshot = state.kalshi_snapshot()
    assert snapshot["state"] == "CORRUPT"
    assert snapshot["discovery_due"] is True


def _advance_selection(state, coverage, selected):
    observed = {row["ticker"] for row in selected}
    state.record_kalshi_refresh(
        NOW,
        observed_tickers=observed,
        actionable_tickers=set(state.kalshi_snapshot()["buy_tickers"]) & observed,
        next_buy_cursor=coverage["next_buy_cursor"],
        next_other_cursor=coverage["next_other_cursor"],
        next_warm_cursor=coverage["next_warm_cursor"],
    )


def test_hot_takes_all_capacity_before_warm_and_reports_partial(tmp_path):
    state = NFLRuntimeState(tmp_path, clock=lambda: NOW)
    hot = [{"ticker": f"HOT-{i:02d}", "scheduled_start": "2026-10-04T17:00:00Z"} for i in range(14)]
    warm = [{"ticker": f"WARM-{i:02d}", "scheduled_start": "2026-10-05T17:00:00Z"} for i in range(3)]
    state.record_kalshi_discovery(hot + warm, actionable_tickers={"HOT-00"}, refreshed_at=NOW)
    selected, coverage = state.kalshi_refresh_rows(state.kalshi_snapshot())
    assert len(selected) == 12
    assert selected[0]["ticker"] == "HOT-00"
    assert all(row["ticker"].startswith("HOT") for row in selected)
    assert (coverage["hot_known_count"], coverage["hot_selected_count"], coverage["hot_omitted_count"]) == (14, 12, 2)
    assert (coverage["warm_known_count"], coverage["warm_selected_count"], coverage["warm_omitted_count"]) == (3, 0, 3)
    assert coverage["hot_coverage_state"] == "PARTIAL"
    assert coverage["continuous_coverage"] is False


def test_hot_nonbuy_rotation_is_fair_even_after_fixed_earlier_window(tmp_path):
    state = NFLRuntimeState(tmp_path, clock=lambda: NOW)
    early = [{"ticker": f"EARLY-{i:02d}", "scheduled_start": "2026-10-04T17:00:00Z"} for i in range(11)]
    later = [{"ticker": f"LATE-{i:02d}", "scheduled_start": "2026-10-04T20:00:00Z"} for i in range(3)]
    state.record_kalshi_discovery(early + later, actionable_tickers=set(), refreshed_at=NOW)
    seen = set()
    for cycle in range(3):
        selected, coverage = state.kalshi_refresh_rows(state.kalshi_snapshot())
        if cycle == 0:
            assert {row["ticker"] for row in early} <= {row["ticker"] for row in selected}
        seen.update(row["ticker"] for row in selected)
        assert coverage["hot_coverage_state"] == "PARTIAL"
        _advance_selection(state, coverage, selected)
    assert seen == {row["ticker"] for row in early + later}


def test_oversubscribed_earlier_hot_group_cannot_starve_later_hot_group(tmp_path):
    state = NFLRuntimeState(tmp_path, clock=lambda: NOW)
    buy = {"ticker": "BUY", "scheduled_start": "2026-10-04T17:00:00Z"}
    early = [{"ticker": f"EARLY-{i:02d}", "scheduled_start": "2026-10-04T17:00:00Z"} for i in range(12)]
    later = [{"ticker": f"LATE-{i:02d}", "scheduled_start": "2026-10-04T20:00:00Z"} for i in range(3)]
    state.record_kalshi_discovery([buy] + early + later, actionable_tickers={"BUY"}, refreshed_at=NOW)

    seen = set()
    for cycle in range(3):
        selected, coverage = state.kalshi_refresh_rows(state.kalshi_snapshot())
        tickers = {row["ticker"] for row in selected}
        assert len(selected) == KALSHI_REFRESH_MAX_MARKETS
        assert selected[0]["ticker"] == "BUY"
        assert coverage["priority_buy_selected"] == 1
        assert coverage["hot_coverage_state"] == "PARTIAL"
        assert coverage["continuous_coverage"] is False
        if cycle == 0:
            assert len(tickers & {row["ticker"] for row in early}) == 11
        seen.update(tickers)
        _advance_selection(state, coverage, selected)

    assert seen == {row["ticker"] for row in [buy] + early + later}


def test_pinned_hot_buy_leaves_rotating_slot_for_watch_and_pass(tmp_path):
    state = NFLRuntimeState(tmp_path, clock=lambda: NOW)
    rows = [{"ticker": f"HOT-{i:02d}", "scheduled_start": "2026-10-04T17:00:00Z"} for i in range(16)]
    buys = {row["ticker"] for row in rows[:12]}
    state.record_kalshi_discovery(rows, actionable_tickers=buys, refreshed_at=NOW)
    seen_nonbuys = set()
    for _ in range(4):
        selected, coverage = state.kalshi_refresh_rows(state.kalshi_snapshot())
        assert coverage["priority_buy_selected"] == 11
        seen_nonbuys.update(row["ticker"] for row in selected if row["ticker"] not in buys)
        _advance_selection(state, coverage, selected)
    assert seen_nonbuys == {row["ticker"] for row in rows[12:]}


def test_warm_uses_only_spare_slots_and_rotates(tmp_path):
    state = NFLRuntimeState(tmp_path, clock=lambda: NOW)
    hot = [{"ticker": f"HOT-{i}", "scheduled_start": "2026-10-04T17:00:00Z"} for i in range(11)]
    warm = [{"ticker": f"WARM-{i}", "scheduled_start": "2026-10-05T17:00:00Z"} for i in range(4)]
    state.record_kalshi_discovery(hot + warm, actionable_tickers=set(), refreshed_at=NOW)
    seen = set()
    for _ in range(4):
        selected, coverage = state.kalshi_refresh_rows(state.kalshi_snapshot())
        assert coverage["hot_selected_count"] == 11
        assert coverage["warm_selected_count"] == 1
        seen.update(row["ticker"] for row in selected if row["ticker"].startswith("WARM"))
        _advance_selection(state, coverage, selected)
    assert seen == {row["ticker"] for row in warm}


def test_kickoff_removes_hot_market_and_eastern_day_sets_tier(tmp_path):
    clock = [datetime(2026, 10, 5, 3, 59, tzinfo=UTC)]  # Sunday 23:59 ET
    state = NFLRuntimeState(tmp_path, clock=lambda: clock[0])
    state.record_kalshi_discovery(
        [{"ticker": "SUNDAY", "scheduled_start": "2026-10-05T04:30:00Z"}],
        actionable_tickers=set(), refreshed_at=clock[0],
    )
    _, before = state.kalshi_refresh_rows(state.kalshi_snapshot())
    assert before["warm_known_count"] == 1
    clock[0] += timedelta(minutes=1)
    _, midnight = state.kalshi_refresh_rows(state.kalshi_snapshot())
    assert midnight["hot_known_count"] == 1
    clock[0] += timedelta(minutes=30)
    selected, started = state.kalshi_refresh_rows(state.kalshi_snapshot())
    assert selected == []
    assert started["started_market_count"] == 1
