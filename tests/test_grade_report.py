import json
import os
import tempfile
import unittest
from unittest import mock


def rec(subject, market, line, winner, actual, blended_a, picked=False, side="A", sport="NBA",
        bet_type="prop", game="A @ B", odds=-150, gap=0.03):
    sides = [{"side": "A", "label": f"{subject} Over {line} {market}", "model_prob": blended_a,
              "market_prob": blended_a - 0.02, "blended": blended_a, "best_odds": odds,
              "best_book": "FanDuel", "gap": gap if side == "A" else -gap},
             {"side": "B", "label": f"{subject} Under {line} {market}", "model_prob": 1 - blended_a,
              "market_prob": 1 - blended_a + 0.02, "blended": 1 - blended_a, "best_odds": 120,
              "best_book": "DraftKings", "gap": gap if side == "B" else -gap}]
    r = {"sport": sport, "game_id": game, "game": game, "subject": subject, "market": market,
         "inputs": {"bet_type": bet_type, "line": line}, "model_prob_a": blended_a,
         "market_prob_a": blended_a - 0.02, "sides": sides, "best_side": side, "picked": picked,
         "result": None if winner == "pending" else {"actual": actual, "winner": winner}}
    if picked:
        r.update(pick_side=side, pick_odds=odds, pick_time="2026-10-22T20:00:00",
                 pick_market_prob=blended_a - 0.02, pick_book="FanDuel")
    return r


class GradeReportTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"LINEMAKER_TRACKING_DIR": self.dir,
                                                "LINEMAKER_WEIGHTS_FILE": os.path.join(self.dir, "w.json")})
        self.env.start()
        recs = {f"k{i}": r for i, r in enumerate([
            rec("LeBron", "points", 24.5, "A", 31, 0.78, picked=True),
            rec("Curry", "threes", 4.5, "B", 3, 0.72, picked=True),
            rec("Tatum", "rebounds", 8.5, None, 8.5, 0.6),
            rec("Brown", "points", 22.5, "A", 25, 0.65),
            rec("Late", "points", 10.5, "pending", None, 0.6, game="C @ D"),
        ])}
        with open(os.path.join(self.dir, "2026-10-22.json"), "w") as f:
            json.dump(recs, f)

    def tearDown(self):
        self.env.stop()

    def test_report(self):
        from linemaker.grade_report import grade_report
        embeds, table = grade_report("2026-10-22", ["- ⚖️ Model weight 0.50 → 0.55 (120 outcomes)",
                                                    "- 🧾 Settled **2** of your logged bets"], set(), 19000)
        text = "\n".join((e.title or "") + "\n" + (e.description or "") for e in embeds)
        self.assertIn("4 of 5", text)                       # graded vs tracked
        self.assertIn("**4** new this run", text)
        self.assertIn("1-1", text)                          # picks record
        self.assertIn("✅ LeBron Over 24.5 points", text)
        self.assertIn("31 vs line 24.5", text)
        self.assertIn("❌ Curry Over 4.5 threes", text)
        self.assertIn("Calibration", text)
        self.assertIn("Model weight 0.50 → 0.55", text)
        self.assertIn("Not graded yet (1 market, 1 game)", text)
        self.assertIn("C @ D", text)
        self.assertIn("Pushes:** 1", text)
        self.assertIn("Surprise misses", text)              # Curry at 72%
        self.assertIn("19,000", text)
        self.assertEqual(table.count("\n"), 5)              # header + 4 graded
        self.assertTrue(all(len(e.description or "") <= 4096 for e in embeds))


if __name__ == "__main__":
    unittest.main()
