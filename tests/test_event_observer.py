from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from scripts.event_observer import (
    MarketTarget,
    build_parser,
    ensure_schema,
    main,
    observation_from_market,
    observe_once,
    persist_observations,
    run_observer,
    validate_window,
)


class EventObserverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        ensure_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_cli_requires_targets_and_validates_window_and_interval(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            main(["--event-label", "e", "--start", "2026-01-01T00:00:00Z", "--end", "2026-01-01T01:00:00Z"])
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            validate_window(start, start, 60)
        with self.assertRaises(ValueError):
            validate_window(start, datetime(2026, 1, 1, 1, tzinfo=timezone.utc), 29)

    def test_multiple_markets_are_polled(self) -> None:
        adapter = Mock()
        adapter.get_market.side_effect = [
            {"id": "1", "slug": "one", "bestBid": 0.4, "bestAsk": 0.5},
            {"id": "2", "slug": "two", "bestBid": 0.6, "bestAsk": 0.7},
        ]
        result = observe_once(
            self.conn, adapter, [MarketTarget(slug="one"), MarketTarget(slug="two")],
            event_label="test", observed_at="2026-01-01T00:00:00+00:00",
        )
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(adapter.get_market.call_count, 2)

    def test_successful_observation_and_duplicate_are_idempotent(self) -> None:
        row = observation_from_market(
            {"id": "1", "slug": "one", "bestBid": "0.4", "bestAsk": "0.5", "lastTradePrice": "0.45", "updatedAt": "2026-01-01T00:00:01Z"},
            event_label="test", observed_at="2026-01-01T00:00:00+00:00", created_at="2026-01-01T00:00:00+00:00",
        )
        self.assertEqual(persist_observations(self.conn, [row])["inserted"], 1)
        self.assertEqual(persist_observations(self.conn, [row])["duplicates"], 1)
        saved = self.conn.execute("SELECT best_bid,best_ask,midpoint,spread,fetch_status FROM event_microstructure_snapshots").fetchone()
        self.assertEqual(saved[:3], (0.4, 0.5, 0.45))
        self.assertAlmostEqual(saved[3], 0.1)
        self.assertEqual(saved[4], "ok")

    def test_fetch_failure_is_recorded_and_does_not_isolate_other_markets(self) -> None:
        adapter = Mock()
        adapter.get_market.side_effect = [TimeoutError("slow"), {"id": "2", "slug": "two", "bestBid": 0.2, "bestAsk": 0.3}]
        result = observe_once(
            self.conn, adapter, [MarketTarget(slug="one"), MarketTarget(slug="two")],
            event_label="test", observed_at="2026-01-01T00:00:00+00:00",
        )
        self.assertEqual(result["inserted"], 2)
        statuses = [row[0] for row in self.conn.execute("SELECT fetch_status FROM event_microstructure_snapshots ORDER BY id")]
        self.assertEqual(statuses[0], "error:TimeoutError")
        self.assertEqual(statuses[1], "ok")

    def test_short_run_stops_after_requested_cycles_and_dry_run_writes_nothing(self) -> None:
        adapter = Mock()
        adapter.get_market.return_value = {"id": "1", "slug": "one"}
        current = [datetime(2026, 1, 1, tzinfo=timezone.utc)]

        def advance(seconds: float) -> None:
            current[0] += timedelta(seconds=seconds)

        cycles = run_observer(
            self.conn, adapter, [MarketTarget(slug="one")], event_label="test",
            start=current[0], end=datetime(2026, 1, 1, 1, tzinfo=timezone.utc), interval=30,
            now=lambda: current[0], sleep=advance, max_cycles=2, dry_run=True,
        )
        self.assertEqual(cycles, 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM event_microstructure_snapshots").fetchone()[0], 0)

    def test_end_of_window_stops_without_fetching(self) -> None:
        adapter = Mock()
        now = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
        cycles = run_observer(
            self.conn, adapter, [MarketTarget(slug="one")], event_label="test",
            start=datetime(2026, 1, 1, tzinfo=timezone.utc), end=now, interval=30,
            now=lambda: now,
        )
        self.assertEqual(cycles, 0)
        adapter.get_market.assert_not_called()

    def test_observer_source_has_no_inference_or_trading_call(self) -> None:
        from pathlib import Path
        source = Path(__file__).parents[1].joinpath("scripts/event_observer.py").read_text()
        self.assertNotIn("LLM", source.upper())
        self.assertNotIn("place_order", source.lower())
        self.assertIn("PolymarketAdapter", source)


if __name__ == "__main__":
    unittest.main()
