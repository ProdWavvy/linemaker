import os
import unittest

from linemaker.config import Config
from linemaker.market import Market, load_markets
from linemaker.model import estimate
from linemaker.odds import fair_odds, implied_probability
from linemaker.pipeline import run

SAMPLE = os.path.join(os.path.dirname(__file__), "..", "data", "sample_slate.csv")


def mk(**kw):
    base = dict(date="2026-10-21", sport="NBA", game_id="G", game="A @ B", start_confirmed=True,
                bet_type="prop", subject="P", team="A", market="points", side="over", line=10.5,
                odds=-250, book="DK", season_hits=22, season_games=24)
    base.update(kw)
    return Market(**base)


class OddsTest(unittest.TestCase):
    def test_implied(self):
        self.assertAlmostEqual(implied_probability(-250), 250 / 350)
        self.assertAlmostEqual(implied_probability(150), 100 / 250)
        with self.assertRaises(ValueError):
            implied_probability(50)

    def test_fair_odds_roundtrip(self):
        self.assertEqual(fair_odds(implied_probability(-300)), -300)
        self.assertEqual(fair_odds(0.4), 150)


class ModelTest(unittest.TestCase):
    def test_hot_streak_never_raises_above_season(self):
        # Handoff example: 91.6% season, 100% recent -> ~88%, not 95%.
        ev = estimate(mk(l5=1.0, l10=1.0), Config())
        self.assertLess(ev.true_prob, ev.season_rate)
        self.assertAlmostEqual(ev.true_prob, 22 / 24 - 0.03, places=4)

    def test_combo_gets_extra_haircut(self):
        plain = estimate(mk(), Config()).true_prob
        combo = estimate(mk(is_combo=True), Config()).true_prob
        self.assertAlmostEqual(plain - combo, Config().combo_haircut)


class PipelineTest(unittest.TestCase):
    def test_sample_slate(self):
        res = run(load_markets(SAMPLE), "2026-10-21", Config())
        picks = [e.market.subject for e in res.picks]
        reasons = {m.subject: r for m, r in res.excluded}
        self.assertIn("Player A", picks)
        self.assertNotIn("Player D", picks)          # 43% season despite hot streak
        self.assertIn("slate", reasons["Player E"])  # tomorrow
        self.assertIn("outside", reasons["Example FC vs Example United"])
        self.assertIn("out", reasons["Player F"])
        gaps = [e.gap for e in res.picks]
        self.assertEqual(gaps, sorted(gaps, reverse=True))
        self.assertTrue(all(e.gap > 0 and e.true_prob >= 0.70 for e in res.picks))


if __name__ == "__main__":
    unittest.main()
