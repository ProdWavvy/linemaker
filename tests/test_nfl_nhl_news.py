import unittest
from unittest import mock

from linemaker import adjustments, alerts, nhl_goalies


def log(dates, **stats):
    return [{"date": d, "stats": dict(stats)} for d in dates]


DATES = ["2026-10-19", "2026-10-12", "2026-10-05", "2026-09-28", "2026-09-21"]


class NFLUsageTest(unittest.TestCase):
    def test_wr1_out_feeds_the_other_receivers(self):
        outs = {"WR One": log(DATES, receptions=7, receivingYards=95)}
        mates = {"WR Two": log(DATES, receptions=4, receivingYards=50),
                 "Tight End": log(DATES, receptions=3, receivingYards=30)}
        b = adjustments.usage_boosts(outs, mates, "NFL")
        self.assertGreater(b["WR Two"]["receptions"], b["Tight End"]["receptions"])   # by share
        self.assertLessEqual(b["WR Two"]["receiving yards"], 0.35 * 50 + 1e-9)        # capped
        self.assertEqual(b["WR Two"]["_why"], ["WR One"])

    def test_depth_player_and_long_absence_ignored(self):
        mates = {"WR Two": log(DATES, receptions=4, receivingYards=50)}
        self.assertEqual(adjustments.usage_boosts({"Depth": log(DATES, receptions=1)}, mates, "NFL")["WR Two"], {})
        old = log(DATES[2:], receptions=7, receivingYards=95)   # missed the last 2 games
        self.assertEqual(adjustments.usage_boosts({"WR One": old}, mates, "NFL")["WR Two"], {})

    def test_backup_qb(self):
        mates = {"WR Two": log(DATES, receptions=4)}
        self.assertEqual(adjustments.backup_qb({"QB One": log(DATES, passingAttempts=33)}, mates), "QB One")
        self.assertIsNone(adjustments.backup_qb({"RB": log(DATES, rushingAttempts=15)}, mates))


class GoalieTest(unittest.TestCase):
    def test_changes(self):
        prev = {"bruins": ["Ullmark", False]}
        self.assertEqual(alerts.goalie_changes(None, {"bruins": ["Korpisalo", True]}), [])
        self.assertEqual(alerts.goalie_changes(prev, {"bruins": ["Korpisalo", False]}), [("bruins", "Korpisalo")])
        self.assertEqual(alerts.goalie_changes(prev, {"bruins": ["Ullmark", True]}), [])

    def test_skater_factor_bounds(self):
        self.assertAlmostEqual(nhl_goalies.skater_factor(0.6), 1.2)
        self.assertEqual(nhl_goalies.skater_factor(5), 1.4)
        self.assertEqual(nhl_goalies.skater_factor(-5), 0.7)

    def test_starter_from_box_score(self):
        box = {"boxscore": {"players": [{"team": {"displayName": "Boston Bruins"}, "statistics": [
            {"keys": ["saves", "timeOnIce"], "athletes": [
                {"athlete": {"displayName": "Backup", "id": 2}, "stats": ["5", "12:30"]},
                {"athlete": {"displayName": "Starter", "id": 1}, "stats": ["25", "47:30"]}]}]}]}}
        self.assertEqual(nhl_goalies._starter(box, "Boston Bruins"), ("Starter", "1"))

    def test_tandem_gets_no_usual_starter(self):
        box = lambda who: {"boxscore": {"players": [{"team": {"displayName": "T"}, "statistics": [
            {"keys": ["saves", "timeOnIce"], "athletes": [{"athlete": {"displayName": who, "id": who}, "stats": ["20", "60:00"]}]}]}]}}
        games = [("d", str(i)) for i in range(10)]
        split = {str(i): box("A" if i % 2 else "B") for i in range(10)}
        with mock.patch("linemaker.dvp._finished", return_value=games), \
                mock.patch("linemaker.dvp._summary", side_effect=lambda s, e: split[e]):
            self.assertIsNone(nhl_goalies.usual_starter("T", "1", "2026-10-24"))
        clear = {str(i): box("A" if i < 8 else "B") for i in range(10)}
        with mock.patch("linemaker.dvp._finished", return_value=games), \
                mock.patch("linemaker.dvp._summary", side_effect=lambda s, e: clear[e]):
            self.assertEqual(nhl_goalies.usual_starter("T", "1", "2026-10-24"), ("A", "A", 8, 10))

    def test_effects_only_for_a_non_usual_starter(self):
        tonight = {"t": {"team": "T", "name": "Backup", "id": "2", "confirmed": True, "team_id": "1", "game": "g"},
                   "u": {"team": "U", "name": "Starter U", "id": "3", "confirmed": False, "team_id": "4", "game": "g"}}
        usual = {"T": ("Starter", "1", 8, 10), "U": ("Starter U", "3", 9, 10)}
        sv = {"1": 0.915, "2": 0.895, "3": 0.91}
        with mock.patch.object(nhl_goalies, "tonight", return_value=tonight), \
                mock.patch.object(nhl_goalies, "usual_starter", side_effect=lambda t, i, d: usual[t]), \
                mock.patch.object(nhl_goalies, "save_pct", side_effect=lambda i: sv[i]):
            eff = nhl_goalies.goalie_effects("2026-10-24")
        self.assertEqual(list(eff), ["t"])
        self.assertAlmostEqual(eff["t"]["extra"], 0.02 * 29)
        self.assertIn("Backup in net (confirmed) instead of Starter", eff["t"]["note"])


if __name__ == "__main__":
    unittest.main()
