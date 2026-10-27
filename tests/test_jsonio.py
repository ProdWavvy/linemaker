import json
import os
import tempfile
import unittest
from unittest import mock

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
            seg = os.path.join(d, "NBA-props", "2026-10-25.json")
            nba = {"sport": "NBA", "inputs": {"bet_type": "prop"}}
            save_json(seg, {"k1": dict(nba, result=None), "k2": dict(nba, x=1)})
            t = Tracker(d, "2026-10-25")
            t._ensure("NBA-props")
            # live grading saves a result while the scan runs
            save_json(seg, {"k1": dict(nba, result={"winner": "A"}), "k2": dict(nba, x=1), "k3": dict(nba, y=2)})
            t.records["k1"] = dict(nba, result=None, new=True)
            t._touched.add("k1")
            t.save()
            with open(seg) as f:
                disk = json.load(f)
            self.assertEqual(disk["k1"]["result"], {"winner": "A"})
            self.assertTrue(disk["k1"]["new"])
            self.assertIn("k3", disk)


class KeepModelTest(unittest.TestCase):
    def test_market_scan_keeps_model_numbers(self):
        from linemaker.pro import ProConfig, market_config, rows_to_markets, run_pro
        from linemaker.tracking import Tracker
        rows = [{"date": "2026-10-25", "sport": "NBA", "game_id": "g1", "game": "A @ H", "bet_type": "prop",
                 "subject": "P", "market": "points", "line": "20.5", "mean": "26", "stdev": "5",
                 "books": "FanDuel:-110/-110|DraftKings:-112/-108|BetMGM:-110/-110|Caesars:-115/-105|Pinnacle:-108/-112"}]
        with tempfile.TemporaryDirectory() as d:
            run_pro(rows_to_markets(rows), "2026-10-25", ProConfig(), None, Tracker(d, "2026-10-25"))
            from linemaker.tracking import load_day
            before = load_day(d, "2026-10-25")
            run_pro(rows_to_markets(rows), "2026-10-25", market_config(), None, Tracker(d, "2026-10-25"))
            after = load_day(d, "2026-10-25")
        k = next(iter(before))
        self.assertGreater(before[k]["model_weight"], 0)
        self.assertEqual(after[k]["model_weight"], before[k]["model_weight"])
        self.assertEqual(after[k]["sides"], before[k]["sides"])


class BackgroundMarketEdgesTest(unittest.TestCase):
    ROWS = [{"date": "2026-10-25", "sport": "NBA", "game_id": "g1", "game": "A @ H", "bet_type": "prop",
             "subject": "P", "market": "points", "line": "20.5", "mean": "26", "stdev": "5",
             "books": "FanDuel:+100/-120|DraftKings:-130/+110|BetMGM:-135/+115|Caesars:-135/+115|Pinnacle:-140/+120"}]

    def test_min_gap_is_four_points(self):
        from linemaker.pro import market_config
        with mock.patch.dict(os.environ, {"LINEMAKER_MARKET_MIN_GAP": ""}):
            self.assertEqual(market_config().min_gap, 0.04)

    def test_logged_from_bought_odds_and_graded_apart(self):
        from linemaker.chat import played_markets, track_market_edges
        from linemaker.grade_report import graded_csv
        from linemaker.pro import ProConfig, rows_to_markets, run_pro
        from linemaker.tracking import Tracker, load_day
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {
                "LINEMAKER_TRACKING": "on", "LINEMAKER_TRACKING_DIR": d, "LINEMAKER_MARKET_MIN_GAP": ""}):
            run_pro(rows_to_markets(self.ROWS), "2026-10-25", ProConfig(max_picks=0), None,
                    Tracker(d, "2026-10-25"))                             # the ask: priced, no model pick
            before = load_day(d, "2026-10-25")
            track_market_edges(self.ROWS, "2026-10-25", wait=True)
            rec = next(iter(load_day(d, "2026-10-25").values()))
            self.assertEqual(rec["market_pick"]["side"], "A")             # FanDuel's over beat the consensus
            self.assertTrue(rec["market_pick"]["background"])
            self.assertEqual(rec["sides"], next(iter(before.values()))["sides"])   # model numbers kept
            self.assertEqual(played_markets("NBA", "2026-10-25", {"g1"}), set())   # no closing read for it
            with mock.patch.dict(os.environ, {"LINEMAKER_MARKET_TRACKING": "off"}):
                os.remove(next(os.path.join(r, f) for r, _d, fs in os.walk(d) for f in fs if f.endswith(".json")))
                track_market_edges(self.ROWS, "2026-10-25", wait=True)
                self.assertEqual(load_day(d, "2026-10-25"), {})
        rec["result"] = {"actual": 25, "winner": "A"}
        lines = graded_csv({"main": {"k": rec}}).splitlines()
        head, row = lines[0].split(","), lines[1].split(",")
        got = dict(zip(head, row))
        self.assertEqual((got["market_pick"], got["market_pick_odds"], got["market_pick_book"],
                          got["market_pick_result"]), ("background", "100", "FanDuel", "hit"))
