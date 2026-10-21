"""ESPN game-log projection model (synthetic game logs, no network)."""
import unittest
from unittest import mock

from linemaker import espn_projections as ep


def nba_games(n=30, minutes=34.0, points=25.0, reb=6.0, ast=5.0, threes=2.5):
    return [{"date": f"2026-0{1 + i % 9}-1{i % 9}", "home": i % 2 == 0, "opponent": "X",
             "stats": {"minutes": minutes, "points": points + (i % 5 - 2) * 3,
                       "totalRebounds": reb, "assists": ast, "threePointFieldGoalsMade": threes}}
            for i in range(n)]


class ModelTest(unittest.TestCase):
    def test_basketball_minutes_times_rate(self):
        mean, sd, steps = ep.basketball_mean(nba_games(), "points", {})
        self.assertAlmostEqual(mean, 25.0, delta=0.6)
        self.assertGreater(sd, 2)
        self.assertTrue(any("opportunity" in s[0] for s in steps))

    def test_defense_and_pace_move_points(self):
        base = ep.basketball_mean(nba_games(), "points", {})[0]
        soft = ep.basketball_mean(nba_games(), "points", {"pace": 1.05, "defense": 1.06})[0]
        self.assertGreater(soft, base)
        reb = ep.basketball_mean(nba_games(), "rebounds", {"defense": 1.2})[0]
        self.assertAlmostEqual(reb, 6.0, delta=0.1)           # defense doesn't touch rebounds

    def test_pra_is_sum_of_parts(self):
        games = nba_games()
        parts = sum(ep.basketball_mean(games, m, {})[0] for m in ("points", "rebounds", "assists"))
        self.assertAlmostEqual(ep.basketball_mean(games, "PRA", {})[0], parts)

    def test_football_opportunity_times_efficiency(self):
        games = [{"stats": {"receivingTargets": 8.0, "receivingYards": 72.0, "receptions": 6.0}}
                 for _ in range(6)]
        mean, _sd, _steps = ep.football_mean(games, "receiving yards")
        self.assertAlmostEqual(mean, 72.0, delta=0.5)
        self.assertAlmostEqual(ep.football_mean(games, "receptions")[0], 6.0, delta=0.1)

    def test_thin_history_returns_nothing(self):
        self.assertIsNone(ep.basketball_mean(nba_games(3), "points", {}))

    def test_out_players_skipped_and_flags_passed(self):
        index = {"star guard": ("1", "Home", "Road", True), "hurt guy": ("2", "Home", "Road", True)}
        with mock.patch.object(ep, "roster_index", return_value=index), \
             mock.patch.object(ep, "injuries", return_value={"hurt guy": "out", "star guard": "questionable"}), \
             mock.patch.object(ep, "_team_factors", return_value={}), \
             mock.patch.object(ep, "game_log", return_value=nba_games()):
            rows = ep.espn_projections("NBA", "2026-10-21", {"Star Guard": {"points", "PRA"},
                                                              "Hurt Guy": {"points"}})
        self.assertEqual(sorted(r["market"] for r in rows), ["PRA", "points"])
        self.assertTrue(all(r["subject"] == "Star Guard" for r in rows))
        self.assertEqual(rows[0]["injury_status"], "questionable")
