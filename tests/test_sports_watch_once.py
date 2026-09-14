import unittest
from datetime import datetime, timedelta, timezone

import scripts.sports_watch_once as watch


class SportsWatchOnceTests(unittest.TestCase):

    def setUp(self):
        self.now = datetime(
            2026, 8, 17, 18, 0,
            tzinfo=timezone.utc,
        )

    def test_quota_above_reserve_not_blocked(self):
        self.assertFalse(
            watch.quota_blocked(
                {
                    "remaining": 12,
                    "reset_epoch": 1787011200,
                },
                now=self.now,
            )
        )

    def test_quota_at_reserve_blocks(self):
        self.assertTrue(
            watch.quota_blocked(
                {
                    "remaining": 5,
                    "reset_epoch": 1787097600,
                },
                now=self.now,
            )
        )

    def test_more_than_six_hours_skips(self):
        start = self.now + timedelta(hours=7)
        self.assertIsNone(
            watch.cadence_seconds(start, now=self.now)
        )

    def test_six_to_two_hours_is_hourly(self):
        start = self.now + timedelta(hours=4)
        self.assertEqual(
            watch.cadence_seconds(start, now=self.now),
            3600,
        )

    def test_two_hours_to_30m_is_15m(self):
        start = self.now + timedelta(minutes=90)
        self.assertEqual(
            watch.cadence_seconds(start, now=self.now),
            900,
        )

    def test_inside_30m_is_5m(self):
        start = self.now + timedelta(minutes=20)
        self.assertEqual(
            watch.cadence_seconds(start, now=self.now),
            300,
        )

    def test_recent_scan_is_not_due(self):
        state = {
            "last_scan_at_utc": (
                self.now - timedelta(minutes=5)
            ).isoformat()
        }

        self.assertFalse(
            watch.scan_due(
                now=self.now,
                cadence=900,
                watch_state=state,
            )
        )

    def test_old_scan_is_due(self):
        state = {
            "last_scan_at_utc": (
                self.now - timedelta(minutes=20)
            ).isoformat()
        }

        self.assertTrue(
            watch.scan_due(
                now=self.now,
                cadence=900,
                watch_state=state,
            )
        )


if __name__ == "__main__":
    unittest.main()
