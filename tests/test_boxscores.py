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
        self.assertEqual(market_value("NBA", "turnovers", s), 3.0)
        self.assertEqual(market_value("NBA", "blks + stls", s), market_value("NBA", "blocks", s) + 5.0)
        self.assertIsNone(market_value("NBA", "first basket", s))   # not a tracked market

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


class NewMarketsTest(unittest.TestCase):
    def test_nfl_defense_kicking_and_combos(self):
        d = {"defensive.totalTackles": 9.0, "defensive.soloTackles": 6.0, "defensive.sacks": 1.0}
        self.assertEqual(market_value("NFL", "tackles + assists", d), 9.0)
        self.assertEqual(market_value("NFL", "defensive interceptions", d), 0.0)   # played, none
        k = {"kicking.totalKickingPoints": 11.0, "kicking.fieldGoalsMade": 3.0, "kicking.extraPointsMade": 2.0}
        self.assertEqual(market_value("NFL", "kicking points", k), 11.0)
        q = {"passing.passingYards": 250.0, "rushing.rushingYards": 30.0, "passing.interceptions": 1.0}
        self.assertEqual(market_value("NFL", "pass + rush yards", q), 280.0)
        self.assertEqual(market_value("NFL", "interceptions thrown", q), 1.0)

    def test_mlb_and_nhl(self):
        p = {"pitching.fullInnings.partInnings": 5.2, "pitching.hits": 4.0, "pitching.earnedRuns": 2.0}
        self.assertEqual(market_value("MLB", "pitcher outs", p), 17)
        self.assertEqual(market_value("MLB", "hits allowed", p), 4.0)
        b = {"batting.hits": 2.0, "batting.runs": 1.0, "batting.RBIs": 3.0}
        self.assertEqual(market_value("MLB", "h+r+rbi", b), 6.0)
        g = {"goalies.saves": 31.0}
        self.assertEqual(market_value("NHL", "saves", g), 31.0)

    def test_game_log_values(self):
        from linemaker.gamelogs import _value
        self.assertEqual(_value("MLB", "singles", {"hits": 3, "doubles": 1, "triples": 0, "homeRuns": 1}), 1)
        self.assertEqual(_value("MLB", "pitcher outs", {"innings": 6.1}), 19)
        self.assertEqual(_value("NBA", "blks + stls", {"blocks": 2, "steals": 1}), 3)
