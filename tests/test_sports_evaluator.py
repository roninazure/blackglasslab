import unittest

from sports.clob import TopOfBook
from sports.evaluator import evaluate_two_way_moneyline


class SportsEvaluatorTests(unittest.TestCase):

    def test_yes_side_uses_team_a_ask(self):
        result = evaluate_two_way_moneyline(
            team_a="A",
            team_b="B",
            fair_probability_a=0.60,
            book_a=TopOfBook(0.54, 0.55, 100, 200),
            book_b=TopOfBook(0.44, 0.45, 100, 300),
            stake_usd=10,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.economics.side, "YES")
        self.assertEqual(result.selected_team, "A")
        self.assertAlmostEqual(result.economics.entry_price, 0.55)

    def test_no_side_uses_team_b_ask(self):
        result = evaluate_two_way_moneyline(
            team_a="A",
            team_b="B",
            fair_probability_a=0.40,
            book_a=TopOfBook(0.54, 0.55, 100, 200),
            book_b=TopOfBook(0.44, 0.45, 100, 300),
            stake_usd=10,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.economics.side, "NO")
        self.assertEqual(result.selected_team, "B")
        self.assertAlmostEqual(result.economics.entry_price, 0.45)

    def test_depth_uses_selected_ask(self):
        result = evaluate_two_way_moneyline(
            team_a="A",
            team_b="B",
            fair_probability_a=0.60,
            book_a=TopOfBook(0.54, 0.55, 100, 200),
            book_b=TopOfBook(0.44, 0.45, 100, 300),
            stake_usd=10,
        )

        self.assertAlmostEqual(
            result.executable_depth_usd,
            0.55 * 200,
        )

    def test_missing_ask_rejects(self):
        result = evaluate_two_way_moneyline(
            team_a="A",
            team_b="B",
            fair_probability_a=0.60,
            book_a=TopOfBook(0.54, None, 100, None),
            book_b=TopOfBook(0.44, 0.45, 100, 300),
            stake_usd=10,
        )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
