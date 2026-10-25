import json
import os
import tempfile
import unittest

from linemaker.jsonio import load_json, load_or_quarantine, save_json


class JsonIOTest(unittest.TestCase):
    def test_atomic_save_and_quarantine(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.json")
            save_json(p, {"a": 1})
            self.assertEqual(load_json(p), {"a": 1})
            self.assertEqual(os.listdir(d), ["x.json"])            # no temp files left behind
            with open(p, "w") as f:
                f.write('{"a": 1, "b"')                              # a half-written file
            self.assertEqual(load_or_quarantine(p), {})
            self.assertFalse(os.path.exists(p))
            self.assertTrue(any(n.startswith("x.json.corrupt-") for n in os.listdir(d)))

    def test_tracker_save_keeps_results_written_meanwhile(self):
        from linemaker.tracking import Tracker
        with tempfile.TemporaryDirectory() as d:
            save_json(os.path.join(d, "2026-10-25.json"), {"k1": {"result": None}, "k2": {"x": 1}})
            t = Tracker(d, "2026-10-25")
            # live grading saves a result while the scan runs
            save_json(t.path, {"k1": {"result": {"winner": "A"}}, "k2": {"x": 1}, "k3": {"y": 2}})
            t.records["k1"] = {"result": None, "new": True}
            t._touched.add("k1")
            t.save()
            with open(t.path) as f:
                disk = json.load(f)
            self.assertEqual(disk["k1"]["result"], {"winner": "A"})
            self.assertTrue(disk["k1"]["new"])
            self.assertIn("k3", disk)
