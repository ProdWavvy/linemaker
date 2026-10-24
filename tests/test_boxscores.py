"""Prop grading from ESPN box scores (fixture trimmed from a real 2026 NBA box score)."""
import json
import os
import unittest

from linemaker.boxscores import market_value, parse_box, prop_results

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "espn_nba_summary.json")


class BoxScoreTest(unittest.TestCase):
    def setUp(self):
        with open(FIX) as f:
            self.box = parse_box(json.load(f))

    def test_nba_stats(self):
        s = self.box["paul george"]
        self.assertEqual(market_value("NBA", "points", s), 38)
        self.assertEqual(market_value("NBA", "threes", s), 7)       # from the '7-12' cell
        self.assertEqual(market_value("NBA", "PRA", s), 46)
        self.assertEqual(market_value("NBA", "steals", s), 5.0)     # tracked since combo/defense markets
        self.assertIsNone(market_value("NBA", "turnovers", s))      # not a tracked market

    def test_nfl_zero_when_not_in_table(self):
        s = {"receiving.receivingYards": 107.0, "receiving.receptions": 4.0,
             "receiving.receivingTouchdowns": 1.0}
        self.assertEqual(market_value("NFL", "rushing yards", s), 0.0)
        self.assertEqual(market_value("NFL", "anytime td", s), 1.0)

    def test_prop_results_rows(self):
        records = {
            "a": {"sport": "NBA", "game_id": "oddsapi1", "subject": "Paul George",
                  "market": "points", "inputs": {"bet_type": "prop", "line": 27.5}},
            "b": {"sport": "NBA", "game_id": "oddsapi1", "subject": "Nobody Here",
                  "market": "points", "inputs": {"bet_type": "prop", "line": 10.5}},
            "c": {"sport": "NBA", "game_id": "oddsapi1", "subject": "Home Team",
                  "market": "spread", "inputs": {"bet_type": "spread", "line": -3.5}},
        }
        rows = prop_results(records, "2026-10-20", fetch=lambda sport, date: self.box)
        self.assertEqual(rows, [{"game_id": "oddsapi1", "subject": "Paul George",
                                 "market": "points", "line": "27.5", "actual": "38"}])

    def test_source_failure_does_not_raise(self):
        def broken(sport, date):
            raise OSError("down")
        records = {"a": {"sport": "NBA", "game_id": "g", "subject": "Paul George",
                         "market": "points", "inputs": {"bet_type": "prop", "line": 27.5}}}
        self.assertEqual(prop_results(records, "2026-10-20", fetch=broken), [])


if __name__ == "__main__":
    unittest.main()
