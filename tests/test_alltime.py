import json
import os
import tempfile
import unittest
from unittest import mock


def rec(sport, market, subject, game, won, side="A", odds=-110, picked=True, **extra):
    r = {"sport": sport, "market": market, "subject": subject, "game_id": game, "game": game,
         "inputs": {"bet_type": "prop", "line": 10.5}, "result": {"winner": side if won else ("B" if side == "A" else "A")},
         "picked": picked, "pick_side": side, "pick_odds": odds, "pick_time": f"2026-10-30T{len(subject):02d}:00:00"}
    r.update(extra)
    return r


class AllTimeTest(unittest.TestCase):
    def test_sections(self):
        from linemaker.reports import alltime_sections
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LINEMAKER_TRACKING_DIR": d,
                                                                             "LINEMAKER_CACHE_DIR": d}):
            recs = {}
            for i in range(12):
                recs[f"2026-10-30|g{i}|P{i}|points|10.5"] = rec("NBA", "points", f"P{i}", f"g{i}", i % 3 != 0)
            recs["2026-10-30|g0|P0|rebounds|5.5"] = rec("NBA", "rebounds", "P0", "g0", False, side="B")   # 2nd on P0
            recs["2026-10-30|g9|Q|PRA|30.5"] = rec("NBA", "PRA", "Q", "g9", True, picked=False,
                                                   shadow_pick={"side": "A", "odds": -110, "time": "x"})
            os.makedirs(os.path.join(d, "NBA-props"))
            with open(os.path.join(d, "NBA-props", "2026-10-30.json"), "w") as f:
                json.dump(recs, f)
            nfl = {"2026-10-30|n1|R|receiving yards|50.5": rec("NFL", "receiving yards", "R", "n1", True)}
            os.makedirs(os.path.join(d, "NFL-props"))
            with open(os.path.join(d, "NFL-props", "2026-10-30.json"), "w") as f:
                json.dump(nfl, f)
            sections = dict(alltime_sections("2026-10-31"))
        totals = "\n".join(sections["📊 Totals"])
        self.assertIn("Pre-game picks: **9-5**", totals)                       # 8-4 points + 0-1 reb + 1-0 NFL
        self.assertIn("one per player per game (today's rule): **9-4**", totals)  # P0's second pick dropped
        self.assertTrue(sections["🏟️ By Sport (pre-game)"][0].startswith("NBA") or
                        sections["🏟️ By Sport (pre-game)"][0].startswith("NFL"))
        self.assertIn("NBA points: **8-4**", "\n".join(sections["📈 By Market (pre-game, best first)"]))
        self.assertIn("smaller markets", "\n".join(sections["📈 By Market (pre-game, best first)"]))
        self.assertIn("shadow picks: **1-0**", "\n".join(sections["🔎 Everything Else"]))
