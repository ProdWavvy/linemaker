import json
import os
import unittest

from linemaker import adjustments, dvp
from linemaker.espn_projections import basketball_mean

HERE = os.path.dirname(__file__)


def log(dates, minutes, points, reb=5, ast=4, threes=2):
    return [{"date": d, "stats": {"minutes": minutes, "points": points, "totalRebounds": reb,
                                  "assists": ast, "threePointFieldGoalsMade": threes}} for d in dates]


DATES = [f"2026-11-{d:02d}" for d in range(20, 5, -1)]   # most recent first


class MinutesTest(unittest.TestCase):
    def test_blowout_and_rest(self):
        self.assertEqual(adjustments.minutes_delta(34, -3.5, False), (0.0, []))
        d, why = adjustments.minutes_delta(34, -13.0, True)
        self.assertAlmostEqual(d, -0.3 * 6 - 1.0)
        self.assertEqual(len(why), 2)
        bench, _ = adjustments.minutes_delta(14, 13.0, False)
        self.assertAlmostEqual(bench, 0.9)

    def test_minutes_scale_the_projection(self):
        games = log(DATES, 30, 24)
        base = basketball_mean(games, "points", {})[0]
        cut = basketball_mean(games, "points", {}, -3.0)[0]
        self.assertAlmostEqual(cut, base * 27 / 30, places=3)


class UsageTest(unittest.TestCase):
    def test_star_out_boosts_teammates_in_proportion(self):
        star = log(DATES, 36, 30, ast=8)
        mates = {"Big": log(DATES, 30, 20), "Wing": log(DATES, 26, 10)}
        b = adjustments.usage_boosts({"Star": star}, mates, "NBA")
        share = adjustments.usage_share() * 30
        self.assertAlmostEqual(b["Big"]["points"], min(share * 20 / (114 - 30), 0.2 * 20))
        self.assertGreater(b["Big"]["points"], b["Wing"]["points"])
        self.assertEqual(b["Big"]["_why"], ["Star"])

    def test_long_absence_already_baked_in(self):
        star = log(DATES[5:], 36, 30)                 # hasn't played the last 5 team games
        mates = {"Big": log(DATES, 30, 20)}
        self.assertEqual(adjustments.usage_boosts({"Star": star}, mates), {"Big": {}})

    def test_bench_player_out_changes_nothing(self):
        scrub = log(DATES, 9, 3)
        self.assertEqual(adjustments.usage_boosts({"Scrub": scrub}, {"Big": log(DATES, 30, 20)}),
                         {"Big": {}})


class DvpTest(unittest.TestCase):
    def test_allowed_by_position(self):
        with open(os.path.join(HERE, "fixtures", "espn_nba_summary.json")) as f:
            summary = json.load(f)
        teams = [t["team"]["displayName"] for t in summary["boxscore"]["players"]]
        for t in summary["boxscore"]["players"]:
            for g in t["statistics"]:
                g.setdefault("keys", g.get("names"))
                for i, a in enumerate(g["athletes"]):
                    a["athlete"]["position"] = {"abbreviation": "CGF"[i % 3]}
        got = dvp.allowed_by_position(summary, teams[0])
        self.assertTrue(set(got) <= {"C", "G", "F"} and len(got) >= 2)
        total = sum(v["points"] for v in got.values())
        self.assertEqual(total, 27.0)                  # the trimmed fixture: Pistons 21 + 6


if __name__ == "__main__":
    unittest.main()
