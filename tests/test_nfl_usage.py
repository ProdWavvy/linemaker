import os
import unittest
from unittest import mock

from linemaker import nfl_usage


def games(n, carries=12, ypc=4.0, recent=None, pos="RB"):
    out = [{"week": i + 1, "position": pos, "carries": carries, "rushing_yards": carries * ypc} for i in range(n)]
    if recent is not None:
        for g in out[-2:]:
            g["carries"], g["rushing_yards"] = recent, recent * ypc
    return out


class RushTest(unittest.TestCase):
    def test_bigger_role_lifts_the_projection(self):
        steady = nfl_usage.rush_projection(games(6), "rushing_yards", 48.0)
        self.assertAlmostEqual(steady[0], 48.0)
        bigger = nfl_usage.rush_projection(games(6, recent=20), "rushing_yards", 52.0)
        self.assertGreater(bigger[0], 52.0)
        self.assertEqual(bigger[1], 20)
        huge = nfl_usage.rush_projection(games(6, recent=40), "rushing_yards", 50.0)
        self.assertAlmostEqual(huge[0], 65.0)                      # capped at +30%

    def test_quarterbacks_and_thin_roles_skip(self):
        self.assertIsNone(nfl_usage.rush_projection(games(6, pos="QB"), "rushing_yards", 20.0))
        self.assertIsNone(nfl_usage.rush_projection(games(2, carries=2), "rushing_yards", 5.0))

    def test_apply_matches_name_and_team(self):
        rows = [{"market": "rushing yards", "subject": "Jaylen Warren", "_team": "Pittsburgh Steelers",
                 "mean": "50.000", "notes": "ESPN"},
                {"market": "receiving yards", "subject": "Jaylen Warren", "_team": "Pittsburgh Steelers",
                 "mean": "20.000", "notes": "ESPN"}]
        data = {("jaylen warren", "PIT"): games(6, recent=20), ("jaylen warren", "XXX"): games(6)}
        with mock.patch.dict(os.environ, {"LINEMAKER_OFFLINE": "", "LINEMAKER_NFL_USAGE": "on"}), \
                mock.patch.object(nfl_usage, "weekly", return_value=data):
            self.assertEqual(nfl_usage.apply_usage("2026-11-15", rows), 1)
        self.assertGreater(float(rows[0]["mean"]), 50.0)
        self.assertIn("nflverse workload", rows[0]["notes"])
        self.assertEqual(rows[1]["mean"], "20.000")

    def test_season(self):
        self.assertEqual(nfl_usage.season_of("2026-11-15"), 2026)
        self.assertEqual(nfl_usage.season_of("2027-01-10"), 2026)
