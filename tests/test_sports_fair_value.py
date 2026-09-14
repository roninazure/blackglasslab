import unittest

from sports.devig import (
    decimal_to_implied_probability,
    devig_two_way,
)
from sports.fair_value import (
    BookMoneyline,
    consensus_two_way_moneyline,
)


class SportsFairValueTests(unittest.TestCase):

    def test_decimal_probability(self):
        self.assertAlmostEqual(
            decimal_to_implied_probability(2.0),
            0.5,
        )

    def test_devig_two_way_sums_to_one(self):
        a, b = devig_two_way(1.85, 2.05)
        self.assertAlmostEqual(a + b, 1.0, places=12)

    def test_consensus_uses_multiple_books(self):
        result = consensus_two_way_moneyline([
            BookMoneyline("book-a", 1.80, 2.10),
            BookMoneyline("book-b", 1.82, 2.08),
            BookMoneyline("book-c", 1.78, 2.12),
        ])

        self.assertIsNotNone(result)
        self.assertEqual(result.sample_size, 3)
        self.assertAlmostEqual(
            result.outcome_a_probability
            + result.outcome_b_probability,
            1.0,
            places=12,
        )

    def test_stale_books_are_rejected(self):
        result = consensus_two_way_moneyline([
            BookMoneyline("fresh", 1.80, 2.10, age_seconds=20),
            BookMoneyline("stale", 1.82, 2.08, age_seconds=500),
        ])

        self.assertIsNone(result)

    def test_invalid_odds_do_not_create_fake_consensus(self):
        result = consensus_two_way_moneyline([
            BookMoneyline("bad", 0.9, 2.0),
            BookMoneyline("good", 1.8, 2.1),
        ])

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
