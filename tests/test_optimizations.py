import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock


def pick(i, won, odds=-110, clv_move=-0.01, market="threes", sport="NBA", game=None):
    t0 = "2026-10-20T18:00:00+00:00"
    return {"sport": sport, "market": market, "game_id": game or f"g{i}", "subject": f"p{i}",
            "picked": True, "pick_side": "A", "pick_odds": odds, "pick_time": t0, "pick_book": "FanDuel",
            "pick_market_prob": 0.55, "market_prob_a": 0.55 + clv_move, "updated": "2026-10-20T23:00:00+00:00",
            "history": [{"t": t0, "A": {"FanDuel": odds}, "B": {"FanDuel": -110}},
                        {"t": "2026-10-20T23:00:00+00:00", "A": {"FanDuel": odds}, "B": {"FanDuel": -110}}],
            "inputs": {"bet_type": "prop", "line": 2.5}, "sides": [],
            "result": {"actual": 3 if won else 1, "winner": "A" if won else "B"}}


class PruneTest(unittest.TestCase):
    def test_losing_market_with_bad_clv_is_paused(self):
        from linemaker.autotune import tune_prune
        recs = [pick(i, i % 2 == 0, clv_move=-0.01) for i in range(120)]   # 50% at -110 = losing
        paused, _ = tune_prune(recs)
        self.assertIn("NBA threes", paused)

    def test_losing_but_beating_the_close_is_kept(self):
        from linemaker.autotune import tune_prune
        recs = [pick(i, i % 2 == 0, clv_move=+0.01) for i in range(120)]
        self.assertEqual(tune_prune(recs)[0], {})

    def test_small_sample_kept(self):
        from linemaker.autotune import tune_prune
        self.assertEqual(tune_prune([pick(i, False) for i in range(40)])[0], {})


class MoveTest(unittest.TestCase):
    def test_market_move(self):
        from linemaker.tracking import market_move
        rec = {"history": [{"t": "2026-10-24T15:00:00+00:00", "A": {"Pinnacle": -110}, "B": {"Pinnacle": -110}},
                           {"t": "2026-10-24T18:00:00+00:00", "A": {"Pinnacle": +120}, "B": {"Pinnacle": -140}}]}
        move = market_move(rec, "A")
        self.assertLess(move, -0.05)                 # the over drifted from 50% to ~44%
        self.assertGreater(market_move(rec, "B"), 0.05)
        rec["history"][1]["t"] = "2026-10-24T15:05:00+00:00"
        self.assertIsNone(market_move(rec, "A"))     # too soon to call it a move

    def test_gate_drops_a_pick_the_market_ran_from(self):
        from linemaker.pro import ProConfig, rows_to_markets, run_pro
        from linemaker.tracking import Tracker
        d = tempfile.mkdtemp()
        row = {"date": "2026-10-21", "sport": "NBA", "game_id": "g", "game": "A @ H", "bet_type": "prop",
               "subject": "Mover", "market": "points", "line": "20.5", "mean": "25",
               "books": "FanDuel:-110/-110|Pinnacle:-110/-110"}
        tr = Tracker(d, "2026-10-21")
        run_pro(rows_to_markets([row]), "2026-10-21", ProConfig(n_sims=2000, over_min_gap=None), None, tr)
        rec = tr.records[next(iter(tr.records))]
        rec["history"][0]["t"] = "2026-10-21T12:00:00+00:00"
        rec["commence"] = ""
        moved = dict(row, books="FanDuel:+130/-160|Pinnacle:+140/-170")
        res = run_pro(rows_to_markets([moved]), "2026-10-21", ProConfig(n_sims=2000, over_min_gap=None), None, tr)
        self.assertEqual(res.picks, [])
        self.assertTrue(any("moved" in r for _l, r in res.passes))


class BetCLVTest(unittest.TestCase):
    def test_beat_the_close(self):
        from linemaker import bets
        d = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"LINEMAKER_TRACKING_DIR": d}):
            with open(os.path.join(d, "2026-10-20.json"), "w") as f:
                json.dump({"k": {"commence": "2026-10-20T23:00:00Z", "history": [
                    {"t": "x", "A": {"FanDuel": -110}}, {"t": "y", "A": {"FanDuel": -140}}]}}, f)
            bets.add_bet([{"key": "k", "side": "A", "date": "2026-10-20", "odds": -110, "book": "FanDuel",
                           "label": "x"}], -110, 10, "FanDuel")
            c = bets.clv_summary(None)
        self.assertEqual(c["n"], 1)
        self.assertGreater(c["avg"], 0.05)            # took -110, closed -140


class HealthTest(unittest.TestCase):
    def test_quiet_period(self):
        from linemaker.admin import due_alerts
        sent = {}
        self.assertEqual(len(due_alerts([("credits", "low")], sent, now=1000)), 1)
        self.assertEqual(due_alerts([("credits", "low")], sent, now=2000), [])
        self.assertEqual(len(due_alerts([("credits", "low")], sent, now=1000 + 25 * 3600)), 1)

    def test_errors_and_credits(self):
        from linemaker import admin
        admin._ERRORS.clear()
        for i in range(6):
            admin._ERRORS.append((1000.0, f"boom {i}"))
        with mock.patch("linemaker.oddsapi.key_quotas", return_value=[(1, "…abcd", 900, 19100, None, True)]):
            kinds = [k for k, _m in admin.health_problems(now=1100.0)]
        self.assertIn("credits", kinds)
        self.assertIn("errors", kinds)


class WhatsWorkingTest(unittest.TestCase):
    def test_lines(self):
        from linemaker.reports import whats_working
        recs = [pick(i, i % 3 != 0, market="points") for i in range(30)] + [pick(100 + i, i % 3 == 0) for i in range(30)]
        text = "\n".join(whats_working(recs))
        self.assertIn("Best markets:** NBA points", text)
        self.assertIn("Worst markets:** NBA threes", text)
        self.assertIn("Overs", text)


if __name__ == "__main__":
    unittest.main()
