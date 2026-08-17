import unittest

from census.collector import _classify, _sport_code


class SportsCensusClassificationTests(unittest.TestCase):

    def test_authoritative_mlb_metadata(self):
        event = {
            "title": "Chicago Cubs vs. Washington Nationals",
            "sport": {"sport": "mlb", "name": "MLB"},
        }
        market = {"question": "Chicago Cubs vs. Washington Nationals"}

        engine, category, _, rejection = _classify(market, event)

        self.assertEqual(engine, "sports_event_driven")
        self.assertEqual(category, "mlb")
        self.assertIsNone(rejection)

    def test_ncaab_is_first_class_sport(self):
        event = {
            "title": "NCAA basketball game",
            "sport": {"sport": "ncaab", "name": "NCAA Basketball"},
        }
        market = {"question": "Team A vs Team B"}

        engine, category, _, rejection = _classify(market, event)

        self.assertEqual(engine, "sports_event_driven")
        self.assertEqual(category, "ncaab")
        self.assertIsNone(rejection)

    def test_cfb_is_first_class_sport(self):
        event = {
            "sport": {"sport": "cfb", "name": "College Football"},
        }
        market = {"question": "Team A vs Team B"}

        engine, category, _, _ = _classify(market, event)

        self.assertEqual(engine, "sports_event_driven")
        self.assertEqual(category, "cfb")

    def test_esports_separated_from_traditional_sports(self):
        event = {
            "title": "Team Falcons vs Astralis",
            "sport": {"sport": "cs2", "name": "CS2"},
        }
        market = {"question": "Team Falcons vs Astralis"}

        engine, category, _, _ = _classify(market, event)

        self.assertEqual(engine, "sports_event_driven")
        self.assertEqual(category, "esports")

    def test_unknown_authoritative_sport_is_still_discovered(self):
        event = {
            "sport": {"sport": "cricket", "name": "Cricket"},
        }
        market = {"question": "Team A vs Team B"}

        engine, category, _, _ = _classify(market, event)

        self.assertEqual(engine, "sports_event_driven")
        self.assertEqual(category, "cricket")

    def test_legacy_keyword_fallback_remains(self):
        event = {"title": "NBA game"}
        market = {"question": "NBA winner"}

        engine, category, _, _ = _classify(market, event)

        self.assertEqual(engine, "sports_event_driven")
        self.assertEqual(category, "sports")


if __name__ == "__main__":
    unittest.main()
