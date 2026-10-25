import os
import unittest

from linemaker.chat import ML_ONLY, OTHER_LINES, quick_kind, quick_mode
from linemaker.pro import ml_parlay_config, ml_parlay_legs, rows_to_markets, run_pro, volume_config


def slate(favs):
    rows = []
    for i, f in enumerate(favs):
        dog = int(-f * 0.85) if f < -150 else 105
        rows.append({"date": "2026-10-25", "sport": "NFL", "game_id": f"g{i}", "game": f"A{i} @ H{i}",
                     "bet_type": "moneyline", "subject": f"H{i}", "opponent": f"A{i}", "market": "moneyline",
                     "line": "0", "mean": str(3 + i * 1.5), "stdev": "13.5",
                     "books": f"FanDuel:{f}/+{dog}|DraftKings:{f - 5}/+{dog - 5}|BetMGM:{f}/+{dog}"})
    return rows_to_markets(rows)


class MlParlayTest(unittest.TestCase):
    def test_routing(self):
        for t in ("ML parlay nfl", "nfl moneyline parlay"):
            self.assertEqual((quick_mode(t), quick_kind(t)), ("volume", "games"))
            self.assertTrue(ML_ONLY.search(t) and not OTHER_LINES.search(t))
        self.assertTrue(OTHER_LINES.search("ml and spreads parlay"))

    def test_favourites_make_the_card(self):
        res = run_pro(slate([-150, -200, -250, -300, -380, -120, -1400]), "2026-10-25", volume_config(track=False))
        self.assertEqual(res.picks, [])                     # the volume gap gate turns them all away
        legs = ml_parlay_legs(res, ml_parlay_config())
        odds = [ev.best.best_odds for ev in legs]
        self.assertEqual(len(legs), 4)                      # -120 too short, -1400 too heavy, -150 under 60%
        self.assertTrue(all(-1000 <= o <= -130 for o in odds))
        self.assertEqual(odds, sorted(odds))
        self.assertEqual(odds[0], -380)                # likeliest first
