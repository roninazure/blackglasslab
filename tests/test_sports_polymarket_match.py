import unittest
from datetime import datetime, timezone

from sports.polymarket_match import (
    PolymarketMoneyline,
    match_moneyline,
)


def market(
    *,
    event_id="1",
    market_id="2",
    slug="mlb-stl-cin-2026-08-17",
    team_a="St. Louis Cardinals",
    team_b="Cincinnati Reds",
):
    return PolymarketMoneyline(
        event_id=event_id,
        market_id=market_id,
        condition_id="condition-1",
        slug=slug,
        event_date="2026-08-17",
        team_a=team_a,
        team_b=team_b,
        token_a="token-a",
        token_b="token-b",
        indicative_price_a=0.525,
        indicative_price_b=0.475,
    )


class SportsPolymarketMatchTests(unittest.TestCase):

    def setUp(self):
        self.now = datetime(
            2026, 8, 17, 18, 0,
            tzinfo=timezone.utc,
        )

    def test_matches_unique_pregame_market(self):
        result = match_moneyline(
            home_team="Cincinnati Reds",
            away_team="St. Louis Cardinals",
            start_time="2026-08-17T22:41:00Z",
            polymarket_markets=[market()],
            now=self.now,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.market_id, "2")

    def test_team_order_does_not_matter(self):
        result = match_moneyline(
            home_team="St. Louis Cardinals",
            away_team="Cincinnati Reds",
            start_time="2026-08-17T22:41:00Z",
            polymarket_markets=[market()],
            now=self.now,
        )

        self.assertIsNotNone(result)

    def test_started_game_is_rejected(self):
        result = match_moneyline(
            home_team="Cincinnati Reds",
            away_team="St. Louis Cardinals",
            start_time="2026-08-17T17:42:00Z",
            polymarket_markets=[market()],
            now=self.now,
        )

        self.assertIsNone(result)

    def test_wrong_date_is_rejected(self):
        result = match_moneyline(
            home_team="Cincinnati Reds",
            away_team="St. Louis Cardinals",
            start_time="2026-08-18T22:41:00Z",
            polymarket_markets=[market()],
            now=self.now,
        )

        self.assertIsNone(result)

    def test_ambiguous_polymarket_match_is_rejected(self):
        result = match_moneyline(
            home_team="Cincinnati Reds",
            away_team="St. Louis Cardinals",
            start_time="2026-08-17T22:41:00Z",
            polymarket_markets=[
                market(event_id="1", market_id="2"),
                market(event_id="3", market_id="4"),
            ],
            now=self.now,
        )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
