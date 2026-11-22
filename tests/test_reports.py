import json
import os
import tempfile
import unittest
from unittest import mock

from linemaker import autotune, reports


class ReportTest(unittest.TestCase):
    def test_pick_record_and_weekly(self):
        recs = {"a": {"picked": True, "pick_side": "A", "pick_odds": -150, "result": {"winner": "A"}},
                "b": {"picked": True, "pick_side": "A", "pick_odds": 120, "result": {"winner": "B"}},
                "c": {"picked": True, "pick_side": "B", "pick_odds": -110, "result": {"winner": None}},
                "d": {"picked": False, "result": {"winner": "A"}}}
        r = reports.pick_record(list(recs.values()))
        self.assertEqual((r["won"], r["lost"], r["push"]), (1, 1, 1))
        self.assertAlmostEqual(r["units"], 1 / 1.5 - 1)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"LINEMAKER_TRACKING_DIR": d, "LINEMAKER_WEIGHTS_FILE": ""}):
            with open(os.path.join(d, "2026-10-20.json"), "w") as f:
                json.dump(recs, f)
            text = "\n".join(reports.weekly_lines("2026-10-21"))
        self.assertIn("🎯 **Picks:** Pre-game **1-1-1**", text)
        self.assertIn("🔧 **Tuning:** 0 re-fits this week", text)
        self.assertNotIn("Auto-tuned weights", text)                    # the long list only with details

    def test_live_weight_tunes_on_live_reads_only(self):
        import json
        from tests.test_autotune import records
        recs = records(3000, model_skill=0.05, seed=3)      # live model barely helps
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"LINEMAKER_WEIGHTS_FILE": "", "LINEMAKER_TRACKING_DIR": d}):
            os.makedirs(os.path.join(d, "live", "NBA-props"))
            with open(os.path.join(d, "live", "NBA-props", "2026-10-20.json"), "w") as f:
                json.dump({f"k{i}": r for i, r in enumerate(recs)}, f)
            lines = autotune.run_autotune(d, "2026-10-21")
            segs = autotune.segment_weights()
        self.assertAlmostEqual(segs["live props"]["weight"], 0.10)      # one 0.25 step toward the fit
        self.assertTrue(any(l.startswith("Live props: Overall weight 0.35 → 0.10") for l in lines))
        self.assertEqual(segs["pre props"]["weight"], 0.5)          # pre-game untouched by live reads
        self.assertEqual(segs["live games"]["weight"], 0.35)


if __name__ == "__main__":
    unittest.main()


class MissingGradesTest(unittest.TestCase):
    def test_waiting_void_and_old_open_bets(self):
        import json, tempfile
        from linemaker import bets, missing_grades
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LINEMAKER_TRACKING_DIR": d}):
            os.makedirs(os.path.join(d, "NBA-props"))
            recs = {"k1": {"game": "A @ B", "picked": True, "result": "won"},
                    "k2": {"game": "A @ B", "picked": True},                 # same game graded: void
                    "k3": {"game": "C @ D", "picked": True},                 # nothing from C @ D yet
                    "k4": {"game": "C @ D"}}
            json.dump(recs, open(os.path.join(d, "NBA-props", "2026-11-20.json"), "w"))
            json.dump({"k": {"game": "E @ F", "result": "lost"}}, open(os.path.join(d, "NBA-props", "2026-11-21.json"), "w"))
            bets.add_bet([{"label": "X over", "date": "2026-11-19"}], -110, 5, "FanDuel", "me")
            rep = missing_grades.report("2026-11-22", d)
            text = "\n".join(missing_grades.lines(rep))
        self.assertEqual(rep["fix"], ["2026-11-20"])
        self.assertIn("1 pick not graded", text)
        self.assertIn("C @ D", text)
        self.assertIn("1 pick void", text)
        self.assertIn("Open bet past its games", text)
        self.assertNotIn("11/21", text)
