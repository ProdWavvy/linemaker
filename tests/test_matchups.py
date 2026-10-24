import unittest
from unittest import mock

from linemaker import nfl_defense, nhl_shots


def box(team, opp_groups):
    return {"boxscore": {"players": [
        {"team": {"displayName": team}, "statistics": []},
        {"team": {"displayName": "Opp"}, "statistics": opp_groups}]}}


class NHLShotsTest(unittest.TestCase):
    def test_shots_allowed_sums_opposing_skaters_not_goalies(self):
        b = box("Hurricanes", [
            {"keys": ["goals", "shotsTotal"], "athletes": [{"stats": ["1", "4"]}, {"stats": ["0", "3"]}]},
            {"keys": ["saves", "shotsTotal"], "athletes": [{"stats": ["30", "0"]}]}])
        self.assertEqual(nhl_shots.shots_allowed(b, "Hurricanes"), 7.0)

    def test_factors_shrink_and_cap(self):
        teams = {"A", "B"}
        summaries = {"A": [box("A", [{"keys": ["shotsTotal"], "athletes": [{"stats": ["24"]}]}])] * 10,
                     "B": [box("B", [{"keys": ["shotsTotal"], "athletes": [{"stats": ["34"]}]}])] * 10}
        with mock.patch("linemaker.context._team_ids", return_value={"a": ("1", "A"), "b": ("2", "B")}), \
                mock.patch("linemaker.dvp._team_games", side_effect=lambda s, tid, d, n: [tid] * 10), \
                mock.patch("linemaker.dvp._summary", side_effect=lambda s, e: summaries["A" if e == "1" else "B"][0]):
            f = nhl_shots.shot_factors("2026-10-24", teams)
        self.assertLess(f["a"], 1)
        self.assertGreater(f["b"], 1)
        self.assertAlmostEqual(f["b"], 1 + 10 / 20 * (34 / 29 - 1), places=3)


class NFLDefenseTest(unittest.TestCase):
    def test_allowed_reads_opponent_groups(self):
        b = box("Seahawks", [
            {"name": "passing", "keys": ["completions/passingAttempts", "passingYards"], "athletes": [{"stats": ["29/40", "300"]}]},
            {"name": "rushing", "keys": ["rushingAttempts", "rushingYards"], "athletes": [{"stats": ["7", "27"]}, {"stats": ["5", "13"]}]},
            {"name": "receiving", "keys": ["receptions", "receivingYards"], "athletes": [{"stats": ["7", "69"]}]}])
        got = nfl_defense.allowed(b, "Seahawks")
        self.assertEqual(got, {"completions": 29.0, "passingYards": 300.0, "rushingYards": 40.0,
                               "receptions": 7.0, "receivingYards": 69.0})


if __name__ == "__main__":
    unittest.main()
