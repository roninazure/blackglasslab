import unittest

from sports.devig import american_to_decimal


class SportsOddsProviderTests(unittest.TestCase):

    def test_positive_american_odds(self):
        self.assertAlmostEqual(
            american_to_decimal(150),
            2.5,
        )

    def test_negative_american_odds(self):
        self.assertAlmostEqual(
            american_to_decimal(-200),
            1.5,
        )

    def test_even_money(self):
        self.assertAlmostEqual(
            american_to_decimal(100),
            2.0,
        )

    def test_zero_is_invalid(self):
        with self.assertRaises(ValueError):
            american_to_decimal(0)


if __name__ == "__main__":
    unittest.main()


class FakeHeaders(dict):
    pass


class SportsOddsQuotaTests(unittest.TestCase):

    def test_header_int_parses_integer(self):
        from sports.odds_provider import _header_int

        headers = FakeHeaders({
            "x-ratelimit-remaining": "13",
        })

        self.assertEqual(
            _header_int(headers, "x-ratelimit-remaining"),
            13,
        )

    def test_header_int_missing_is_none(self):
        from sports.odds_provider import _header_int

        self.assertIsNone(
            _header_int(FakeHeaders(), "x-ratelimit-remaining")
        )

    def test_header_int_invalid_is_none(self):
        from sports.odds_provider import _header_int

        headers = FakeHeaders({
            "x-ratelimit-remaining": "unknown",
        })

        self.assertIsNone(
            _header_int(headers, "x-ratelimit-remaining")
        )
