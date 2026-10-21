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
        self.assertIn("**Model picks:** 1-1-1", text)
        self.assertIn("no changes this week", text)

    def test_live_weight_tunes_on_live_reads_only(self):
        from tests.test_autotune import records
        recs = records(3000, model_skill=0.05, seed=3)      # live model barely helps
        w, changes = autotune.tune_live(recs, {"live_weight": 0.35})
        self.assertAlmostEqual(w, 0.25)
        self.assertIn("Live weight 0.35 → 0.25", changes[0])
        self.assertEqual(autotune.tune_live(recs[:50], {}), (0.35, []))


if __name__ == "__main__":
    unittest.main()
