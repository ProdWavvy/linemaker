import json
import os
import tempfile
import unittest
from unittest import mock

from linemaker import bets, staking


class StakeTest(unittest.TestCase):
    def test_kelly(self):
        self.assertAlmostEqual(staking.kelly_fraction(0.55, 100), 0.10)
        with mock.patch.dict(os.environ, {"LINEMAKER_KELLY": "0.25", "LINEMAKER_MAX_STAKE_PCT": "2",
                                          "LINEMAKER_BANKROLL": "1000", "LINEMAKER_STAKE_TIERS": ""}):
            self.assertAlmostEqual(staking.suggested(0.55, 100), 0.02)       # 2.5% capped at 2%
            self.assertAlmostEqual(staking.suggested(0.53, 100), 0.015)
            self.assertEqual(staking.suggested(0.45, 100), 0.0)
            self.assertIn("$15 · 1.5% of your $1,000 bankroll", staking.stake_line(0.53, 100))
            self.assertIn("no edge", staking.stake_line(0.45, 100))
            self.assertEqual(staking.suggested_dollars(0.53, 100), 15.0)
        with mock.patch.dict(os.environ, {"LINEMAKER_BANKROLL": "", "LINEMAKER_STAKE_TIERS": ""}):
            self.assertNotIn("$", staking.stake_line(0.53, 100).split("**")[-1].split("·")[0])


class TierTest(unittest.TestCase):
    def test_your_amounts(self):
        with mock.patch.dict(os.environ, {"LINEMAKER_STAKE_TIERS": "2.5,5,10,15,20",
                                          "LINEMAKER_KELLY": "0.25", "LINEMAKER_MAX_STAKE_PCT": "2"}):
            # quarter-Kelly share of the 2% cap picks the tier
            self.assertEqual(staking.stake_tier(0.53, 100), 15)        # 1.5% of 2% -> 4th of 5
            self.assertEqual(staking.stake_tier(0.60, 100), 20)        # capped -> top
            self.assertEqual(staking.stake_tier(0.505, 100), 2.5)      # thin edge -> bottom
            self.assertIsNone(staking.stake_tier(0.45, 100))           # no edge -> skip
            self.assertEqual(staking.stake_line(0.53, 100), "💵 **Stake:** $15")
            self.assertEqual(staking.stake_line(0.505, 100), "💵 **Stake:** $2.50")
            self.assertIn("skip", staking.stake_line(0.45, 100))
            self.assertEqual(staking.stake_tier(0.70, 100, parlay=True), 10)    # parlays with an edge: up to $10
            self.assertEqual(staking.stake_tier(0.38, 180, parlay=True), 5)     # a smaller edge: $5
            self.assertIn("$2.50 (fun money", staking.stake_line(0.30, 150, parlay=True))
            self.assertEqual(staking.suggested_dollars(0.53, 100), 15)


class BetLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"LINEMAKER_TRACKING_DIR": self.tmp.name})
        self.env.start()
        records = {"k1": {"result": {"winner": "A"}}, "k2": {"result": {"winner": "B"}},
                   "k3": {"result": {"winner": None}}, "k4": {}}
        with open(os.path.join(self.tmp.name, "2026-10-21.json"), "w") as f:
            json.dump(records, f)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def leg(self, key, side="A", odds=-150):
        return {"key": key, "side": side, "label": key, "date": "2026-10-21", "odds": odds, "live": False}

    def test_settle_singles_and_parlays(self):
        bets.add_bet([self.leg("k1")], -150, 30, "FanDuel")                  # won
        bets.add_bet([self.leg("k2")], -110, 11, "DraftKings")               # lost
        bets.add_bet([self.leg("k3")], -110, 10, "FanDuel")                  # push
        bets.add_bet([self.leg("k1"), self.leg("k2", "B", -200)], 125, 10, "FanDuel")   # parlay won
        bets.add_bet([self.leg("k1"), self.leg("k3", "A", -200)], 125, 10, "FanDuel")   # push leg
        bets.add_bet([self.leg("k1"), self.leg("k4")], 125, 10, "FanDuel")   # still open
        self.assertEqual(bets.settle(), 5)
        got = {(b["kind"], b["stake"], b["odds"]): (b["status"], b.get("profit")) for b in bets.load_bets()}
        self.assertEqual(got[("single", 30, -150)], ("won", 20.0))
        self.assertEqual(got[("single", 11, -110)], ("lost", -11))
        self.assertEqual(got[("single", 10, -110)], ("push", 0.0))
        statuses = [b["status"] for b in bets.load_bets() if b["kind"] == "parlay"]
        self.assertEqual(statuses, ["won", "won", "open"])
        pushed = [b for b in bets.load_bets() if b["kind"] == "parlay"][1]
        self.assertAlmostEqual(pushed["profit"], 6.67)                   # paid on the -150 leg alone
        s = bets.summary(None, "2026-10-21")
        self.assertEqual((s["won"], s["lost"], s["push"], len(s["open"])), (3, 1, 1, 1))
        self.assertAlmostEqual(s["profit"], 20 - 11 + 12.5 + 6.67)
        self.assertEqual(bets.settle(), 0)                                  # nothing new


if __name__ == "__main__":
    unittest.main()


class BankrollModeTest(unittest.TestCase):
    def test_stakes_follow_the_bankroll_and_paper_stays_apart(self):
        import tempfile
        from linemaker import bets
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {
                "LINEMAKER_TRACKING_DIR": d, "LINEMAKER_BANKROLL": "30", "LINEMAKER_BANKROLL_SINCE": "2026-01-01",
                "LINEMAKER_KELLY": "0.25", "LINEMAKER_MAX_STAKE_PCT": "2"}):
            self.assertEqual(staking.suggested_dollars(0.55, 100), 0.6)                  # 2% of $30
            self.assertEqual(staking.suggested_dollars(0.40, 100, parlay=True), 0.15)    # fun money 0.5%
            self.assertLessEqual(staking.suggested_dollars(0.6, 150, parlay=True), 0.3)  # parlays: 1% cap
            leg = {"key": "d|g|P|points|20.5", "side": "A", "label": "P o20.5", "date": "2026-10-26"}
            real = bets.add_bet([leg], 100, 10, "FanDuel")
            bets.update_bet(real["id"], status="lost", profit=-10.0, settled="2026-10-26T12:00:00")
            fake = bets.add_bet([leg], 100, 10, "FanDuel", paper=True)
            bets.update_bet(fake["id"], status="won", profit=10.0, settled="2026-10-26T12:00:00")
            self.assertEqual(staking.current_bankroll(), 20.0)                           # practice doesn't count
            self.assertEqual(staking.suggested_dollars(0.55, 100), 0.4)                  # 2% of $20
            self.assertEqual(bets.summary(0, paper=True)["won"], 1)
            self.assertEqual(bets.summary(0)["lost"], 1)


class MaxFavoriteTest(unittest.TestCase):
    def test_no_leg_heavier_than_cap(self):
        from linemaker.pro import ProConfig, rows_to_markets, run_pro, volume_config
        rows = [{"date": "2026-10-26", "sport": "NFL", "game_id": f"g{i}", "game": f"A{i} @ H{i}", "bet_type": "prop",
                 "subject": f"P{i}", "market": "receiving yards", "line": "20.5", "mean": str(45 - i * 4), "stdev": "12",
                 "books": f"FanDuel:{o}/+{max(abs(o) - 60, 110)}|DraftKings:{o}/+{max(abs(o) - 60, 110)}"}
                for i, o in enumerate((-600, -400, -260, -240, -180, -150, -130))]
        with mock.patch.dict(os.environ, {"LINEMAKER_MAX_FAVORITE": "-250"}):
            cfg = volume_config(track=False)
            cfg.min_prob, cfg.min_gap = 0.5, -1.0
            res = run_pro(rows_to_markets(rows), "2026-10-26", cfg)
            self.assertTrue(res.picks)
            self.assertTrue(all(ev.best.best_odds >= -250 for ev in res.picks))
            smart = run_pro(rows_to_markets(rows), "2026-10-26", ProConfig(track=False, min_gap=-1.0))
            self.assertTrue(all(ev.best.best_odds >= -250 for ev in smart.picks))


class BonusBetTest(unittest.TestCase):
    def test_bonus_miss_costs_nothing_win_pays_profit(self):
        import tempfile
        from linemaker import bets
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LINEMAKER_TRACKING_DIR": d}):
            legs = [{"key": "NBA|g1|A|points|10.5", "label": "A Over 10.5 points", "odds": -110, "side": "A"}]
            b1 = bets.add_bet(legs, 300, 10, "FanDuel", "me")
            b2 = bets.add_bet([dict(legs[0], key="NBA|g2|B|points|10.5")], 300, 10, "FanDuel", "me")
            bets.update_bet(b1["id"], bonus=True)
            bets.update_bet(b2["id"], bonus=True)
            with mock.patch("linemaker.bets.leg_outcome", side_effect=lambda l, c: "lost" if "g1" in l["key"] else "won"):
                self.assertEqual(bets.settle(), 2)
            done = {b["id"]: b for b in bets.load_bets()}
            self.assertEqual(done[b1["id"]]["profit"], 0)        # a missed bonus bet loses nothing of yours
            self.assertEqual(done[b2["id"]]["profit"], 30.0)     # a win pays the profit only
            self.assertEqual(bets.summary(None)["staked"], 0)    # none of your money was risked


class LateLiveBetTest(unittest.TestCase):
    def test_after_midnight_live_bet_settles_from_next_days_file(self):
        from types import SimpleNamespace as NS
        from linemaker import grading
        from linemaker.tracking import load_day
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LINEMAKER_TRACKING_DIR": d}), \
                mock.patch.object(grading, "TRACKING", d):
            seg = os.path.join(d, "live", "NBA-games")
            os.makedirs(seg)
            rec = {"sport": "NBA", "game_id": "e1", "game": "Golden State Warriors @ Denver Nuggets",
                   "commence": "2026-10-25T02:00:00Z", "subject": "Golden State Warriors", "market": "spread",
                   "threshold": -26.5, "inputs": {"bet_type": "spread", "line": 26.5},
                   "sides": [], "result": None}
            with open(os.path.join(seg, "2026-10-25.json"), "w") as f:   # read at 12:39 AM: next day's file
                json.dump({"k1": rec}, f)
            bets.add_bet([{"key": "k1", "side": "A", "label": "Golden State Warriors +26.5", "odds": -880,
                           "date": "2026-10-24", "live": True, "sport": "NBA", "bet_type": "spread"}],
                         101, 4.17, "DraftKings", "me")
            self.assertEqual(bets.stale_days("2026-10-28"), ["2026-10-24", "2026-10-25"])
            board = {("golden state warriors", "denver nuggets"): NS(state="post", away_score=110, home_score=118)}
            with mock.patch("linemaker.live_model.live_states", lambda sport, day: board if day == "2026-10-24" else {}):
                self.assertEqual(grading.regrade_live("2026-10-25"), 1)       # ESPN final, by the game's own date
            self.assertIsNotNone(load_day(os.path.join(d, "live"), "2026-10-25")["k1"]["result"])
            self.assertEqual(bets.settle(), 1)
            self.assertEqual(bets.load_bets()[0]["status"], "won")            # lost by 8 with +26.5
