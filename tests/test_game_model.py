import os
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from linemaker import game_model
from linemaker.pro import ProConfig
from tests.test_oddsapi import FakeAPI


def entry(name, gp, pf, pa):
    return {"team": {"displayName": name},
            "stats": [{"name": "wins", "value": gp // 2}, {"name": "losses", "value": gp - gp // 2},
                      {"name": "pointsFor", "value": pf * gp}, {"name": "pointsAgainst", "value": pa * gp}]}


NOW = {"children": [{"standings": {"entries": [entry("Home Hawks", 20, 118, 108),
                                               entry("Road Bulls", 20, 110, 114)]}}]}
LAST = {"children": [{"standings": {"entries": [entry("Home Hawks", 82, 112, 112),
                                                entry("Road Bulls", 82, 112, 112)]}}]}


class GameModelTest(unittest.TestCase):
    def setUp(self):
        tables = {2027: game_model.parse_standings(NOW), 2026: game_model.parse_standings(LAST)}
        self.p = mock.patch.object(game_model, "standings", lambda sport, season: tables[season])
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_parse_and_blend(self):
        t = game_model.parse_standings(NOW)
        self.assertEqual(t["home hawks"], (20, 118.0, 108.0))
        r = game_model.team_ratings("NBA", "2026-12-01")
        w = 20 / 35
        self.assertAlmostEqual(r["home hawks"]["scored"], w * 118 + (1 - w) * 112)
        self.assertIn("43% last season", r["home hawks"]["basis"])

    def test_matchup_margin_and_total(self):
        r = game_model.team_ratings("NBA", "2026-12-01")
        g = game_model.project_game("NBA", "Home Hawks", "Road Bulls", r, set(), None)
        h, a = r["home hawks"], r["road bulls"]
        pts_h = h["scored"] + a["allowed"] - h["avg"]
        pts_a = a["scored"] + h["allowed"] - h["avg"]
        self.assertAlmostEqual(g["margin"], pts_h - pts_a + game_model.HOME_EDGE["NBA"])
        self.assertAlmostEqual(g["total"], pts_h + pts_a)
        tired = game_model.project_game("NBA", "Home Hawks", "Road Bulls", r, {"home hawks"}, None)
        self.assertAlmostEqual(tired["margin"], g["margin"] - game_model.REST_PENALTY["NBA"])

    def test_kalshi_blend(self):
        r = game_model.team_ratings("NBA", "2026-12-01")
        base = game_model.project_game("NBA", "Home Hawks", "Road Bulls", r, set(), None)
        with mock.patch.dict(os.environ, {"LINEMAKER_KALSHI_WEIGHT": "1"}):
            g = game_model.project_game("NBA", "Home Hawks", "Road Bulls", r, set(), 0.5)
        self.assertAlmostEqual(g["margin"], 0.0)       # full weight on a coin-flip market
        with mock.patch.dict(os.environ, {"LINEMAKER_KALSHI_WEIGHT": "0.4"}):
            g = game_model.project_game("NBA", "Home Hawks", "Road Bulls", r, set(), 0.5)
        self.assertAlmostEqual(g["margin"], base["margin"] * 0.6)

    def test_run_games_prices_moneyline_spread_total(self):
        from linemaker import live
        with mock.patch.object(game_model, "played_yesterday", lambda s, d: set()), \
                mock.patch.dict(os.environ, {"LINEMAKER_KALSHI_WEIGHT": "0"}):
            lr = live.run_games(FakeAPI(), ["NBA"], "2026-10-21", ZoneInfo("America/New_York"),
                                ProConfig())
        kinds = sorted({ev.market.bet_type for ev in lr.result.evaluated})
        self.assertEqual(kinds, ["moneyline", "spread", "total"])
        self.assertEqual(lr.result.kind, "games")
        self.assertIn("game model", lr.result.projection_source)


if __name__ == "__main__":
    unittest.main()
