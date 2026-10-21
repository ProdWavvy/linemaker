import datetime as dt
import unittest
import unittest.mock

from linemaker import tracking


def rec(pick_prob, close_prob_a, side="A", later=True):
    hist = [{"t": "2026-10-21T15:00:00+00:00", "A": {"FanDuel": -110}, "B": {"FanDuel": -110}}]
    if later:
        hist.append({"t": "2026-10-21T22:00:00+00:00", "A": {"FanDuel": -130}, "B": {"FanDuel": 110}})
    return {"sport": "NBA", "market": "points", "picked": True, "pick_side": side,
            "pick_time": "2026-10-21T15:00:00+00:00", "pick_odds": -110, "pick_book": "FanDuel",
            "pick_market_prob": pick_prob, "market_prob_a": close_prob_a, "history": hist,
            "updated": hist[-1]["t"]}


class ClvTest(unittest.TestCase):
    def test_market_moved_our_way(self):
        c = tracking.pick_clv(rec(0.50, 0.55))
        self.assertAlmostEqual(c["prob"], 0.05)
        self.assertAlmostEqual(c["price"], 130 / 230 - 110 / 210)
        under = tracking.pick_clv(rec(0.50, 0.55, side="B"))
        self.assertAlmostEqual(under["prob"], -0.05)

    def test_single_read_has_no_clv(self):
        self.assertIsNone(tracking.pick_clv(rec(0.5, 0.5, later=False)))

    def test_summary(self):
        recs = [rec(0.50, 0.53), rec(0.50, 0.49), rec(0.5, 0.5, later=False), {"picked": False}]
        s = tracking.clv_summary(recs)
        self.assertEqual(s["n"], 2)
        self.assertAlmostEqual(s["beat"], 0.5)
        self.assertAlmostEqual(s["avg_prob"], 0.01)
        self.assertIn("beat the close **50%**", tracking.clv_lines(recs)[0])



if __name__ == "__main__":
    unittest.main()


class PickPriceKeptTest(unittest.TestCase):
    def test_pick_price_survives_rescans(self):
        import json
        import os
        import tempfile
        from linemaker.pro import ProConfig, load_pro_markets, run_pro
        from linemaker.tracking import Tracker
        from tests.test_pro import SLATE
        with tempfile.TemporaryDirectory() as d:
            first = dt.datetime(2026, 10, 21, 15, 0, tzinfo=dt.timezone.utc)
            with unittest.mock.patch.object(tracking, "_now", lambda: first):
                run_pro(load_pro_markets(SLATE), "2026-10-21", ProConfig(n_sims=2000), tracker=Tracker(d, "2026-10-21"))
            run_pro(load_pro_markets(SLATE), "2026-10-21", ProConfig(n_sims=2000), tracker=Tracker(d, "2026-10-21"))
            with open(os.path.join(d, "2026-10-21.json")) as f:
                picked = [r for r in json.load(f).values() if r["picked"]]
        self.assertTrue(picked)
        self.assertTrue(all(r["pick_time"].startswith("2026-10-21T15:00") for r in picked))
        self.assertTrue(all(r["pick_odds"] for r in picked))
