import unittest
from unittest import mock

from linemaker import mlb_matchups as mm


def games(ab, n=10):
    return [{"date": f"2026-09-{i + 1:02d}", "stats": {"atBats": ab}} for i in range(n)]


class MLBTest(unittest.TestCase):
    def test_league_average_pitcher_is_neutral(self):
        rates = {"hits": mm.LEAGUE["hits"], "tb": mm.LEAGUE["tb"], "hr": mm.LEAGUE["hr"], "era": mm.LEAGUE["era"]}
        self.assertAlmostEqual(mm.hitter_factor(rates, "hits"), 1.0)
        self.assertAlmostEqual(mm.extra_runs(rates), 0.0)

    def test_ace_trims_hitters(self):
        ace = {"hits": 0.18, "tb": 0.28, "hr": 0.02, "era": 2.8}
        self.assertLess(mm.hitter_factor(ace, "hits"), 0.9)
        self.assertLess(mm.extra_runs(ace), 0)

    def test_spot_factor(self):
        self.assertGreater(mm.spot_factor(1, games(3.6)), 1.0)      # leadoff: more at-bats
        self.assertEqual(mm.spot_factor(9, games(5.0)), 0.85)       # capped
        self.assertIsNone(mm.spot_factor(3, games(4, 3)))           # thin log

    def test_regression_pulls_short_careers_to_league(self):
        data = {"splits": {"categories": [{"name": "pitching", "stats": [
            {"name": "battersFaced", "value": 30}, {"name": "hits", "value": 2}, {"name": "opponentTotalBases", "value": 3},
            {"name": "homeRuns", "value": 0}, {"name": "earnedRuns", "value": 0}, {"name": "innings", "value": 8}]}]}}
        with mock.patch("linemaker.ufc_model._get", return_value=data):
            r = mm.pitcher_rates("1")
        self.assertGreater(r["hits"], 0.2)          # 2 hits in 30 BF is mostly regressed away
        self.assertGreater(r["era"], 3.9)

    def test_apply_to_props(self):
        from linemaker.espn_projections import _apply_mlb
        sps = {"brewers": {"team": "Brewers", "name": "Ace Starter", "event": "e1",
                           "rates": {"hits": 0.18, "tb": 0.28, "hr": 0.02, "era": 2.8}},
               "rays": {"team": "Rays", "name": "Rays Starter", "event": "e1", "rates": None}}
        rows = [
            {"subject": "Rays Hitter", "market": "hits", "mean": "1.0", "notes": "", "_team": "Rays", "_opp": "Brewers", "_games": games(3.6)},
            {"subject": "Rays Bench", "market": "hits", "mean": "1.0", "notes": "", "_team": "Rays", "_opp": "Brewers", "_games": games(3.6)},
            {"subject": "Old Starter", "market": "strikeouts", "mean": "6", "notes": "", "_team": "Brewers", "_opp": "Rays", "_games": []},
            {"subject": "Ace Starter", "market": "strikeouts", "mean": "6", "notes": "", "_team": "Brewers", "_opp": "Rays", "_games": []}]
        with mock.patch.object(mm, "starters", return_value=sps), \
                mock.patch.object(mm, "lineups", return_value={"rays": {"rays hitter": 1}}):
            _apply_mlb("2026-10-24", rows)
        self.assertIn("batting 1", rows[0]["notes"])
        self.assertIn("vs Ace Starter", rows[0]["notes"])
        self.assertEqual(rows[1]["injury_status"], "inactive")       # not in the posted lineup
        self.assertEqual(rows[2]["injury_status"], "inactive")       # scratched pitcher's Ks
        self.assertNotIn("injury_status", rows[3])


if __name__ == "__main__":
    unittest.main()
