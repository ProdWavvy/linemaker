import unittest
from types import SimpleNamespace as NS
from unittest import mock

from linemaker import live_props


def fm(player, market, line=20.5):
    return NS(bet_type="prop", subject=player, market=market, line=line)


GAME = {"id": "e1", "away": "Knicks", "home": "Celtics", "state": "in", "detail": "Q3 6:00",
        "left": 0.375, "home_left": 0.375, "away_left": 0.375, "lead": 4}


class LivePropsTest(unittest.TestCase):
    def _run(self, game, stats, teams=None):
        pre = [{"subject": "Jalen Brunson", "market": "points", "mean": "28", "stdev": "7"},
               {"subject": "Bench Guy", "market": "points", "mean": "8", "stdev": "4"}]
        with mock.patch.object(live_props, "box", return_value=(stats, teams or {})), \
                mock.patch("linemaker.espn_projections.espn_projections", return_value=pre):
            return live_props.live_prop_rows("NBA", "2026-10-25", game,
                                             [fm("Jalen Brunson", "points"), fm("Bench Guy", "points", 6.5)])

    def test_final_is_so_far_plus_the_rest(self):
        rows = self._run(GAME, {"jalen brunson": {".points": 18.0, ".minutes": 26.0},
                                "bench guy": {".points": 2.0, ".minutes": 9.0}})
        jb = next(r for r in rows if r["subject"] == "Jalen Brunson")
        self.assertAlmostEqual(float(jb["mean"]), 18 + 28 * 0.375, places=2)
        self.assertAlmostEqual(float(jb["stdev"]), 7 * 0.375 ** 0.5, places=2)
        self.assertTrue(jb["notes"].startswith("LIVE · 18 so far"))

    def test_missing_player_after_a_third_is_skipped_and_blowouts_cut_the_rest(self):
        rows = self._run(dict(GAME, left=0.3, lead=30), {"jalen brunson": {".points": 30.0, ".minutes": 30.0}})
        self.assertEqual([r["subject"] for r in rows], ["Jalen Brunson"])       # bench guy not in the box
        self.assertLess(float(rows[0]["mean"]), 30 + 28 * 0.3)
        self.assertIn("rest cut", rows[0]["notes"])

    def test_not_live_or_almost_over(self):
        self.assertEqual(self._run(dict(GAME, state="post"), {}), [])
        self.assertEqual(self._run(dict(GAME, left=0.02), {}), [])


if __name__ == "__main__":
    unittest.main()


class DecidedTest(unittest.TestCase):
    def test_decided_reads(self):
        from linemaker.live_props import decided_record
        rec = {"source": "LIVE · 1 so far, 0.4 more expected (2nd 08:12)", "threshold": 0.5,
               "inputs": {"bet_type": "prop", "line": 0.5}}
        self.assertTrue(decided_record(rec))                     # already has the point
        rec["threshold"] = 1.5
        self.assertFalse(decided_record(rec))
        self.assertFalse(decided_record({"source": "pre-game", "threshold": 0.5,
                                         "inputs": {"bet_type": "prop"}}))
