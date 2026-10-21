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
            self.assertIn("($15)", staking.stake_line(0.53, 100))
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
            self.assertEqual(staking.stake_tier(0.70, 100, parlay=True), 5)     # parlays: $2.50 or $5
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
