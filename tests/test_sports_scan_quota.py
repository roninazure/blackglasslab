import unittest
from datetime import datetime, timezone

import scripts.sports_scan as sports_scan


class SportsScanQuotaTests(unittest.TestCase):

    def setUp(self):
        self.now = datetime(
            2026, 8, 17, 18, 0,
            tzinfo=timezone.utc,
        )

    def test_no_state_allows_request(self):
        self.assertTrue(
            sports_scan.quota_allows_request(
                None,
                now=self.now,
            )
        )

    def test_above_reserve_allows_request(self):
        self.assertTrue(
            sports_scan.quota_allows_request(
                {
                    "remaining": 12,
                    "reset_epoch": 1787011200,
                },
                now=self.now,
            )
        )

    def test_at_reserve_before_reset_blocks(self):
        self.assertFalse(
            sports_scan.quota_allows_request(
                {
                    "remaining": 5,
                    "reset_epoch": 1787097600,
                },
                now=self.now,
            )
        )

    def test_below_reserve_before_reset_blocks(self):
        self.assertFalse(
            sports_scan.quota_allows_request(
                {
                    "remaining": 2,
                    "reset_epoch": 1787097600,
                },
                now=self.now,
            )
        )

    def test_reserve_after_reset_allows_request(self):
        self.assertTrue(
            sports_scan.quota_allows_request(
                {
                    "remaining": 1,
                    "reset_epoch": 1,
                },
                now=self.now,
            )
        )


if __name__ == "__main__":
    unittest.main()
