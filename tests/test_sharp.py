import os
import unittest
from unittest import mock

from linemaker.devig import no_vig
from linemaker.pro import ProConfig, market_probs, rows_to_markets


def market(books):
    return rows_to_markets([{"date": "2026-10-21", "sport": "NBA", "game_id": "g", "game": "A @ H",
                             "bet_type": "moneyline", "subject": "H", "opponent": "A",
                             "market": "moneyline", "line": "0", "mean": "3", "books": books}])[0]


class SharpTest(unittest.TestCase):
    def test_pinnacle_counts_triple_by_default(self):
        m = market("FanDuel:-150/+130|DraftKings:-150/+130|Pinnacle:-180/+160")
        soft, sharp = no_vig(-150, 130)[0], no_vig(-180, 160)[0]
        probs = market_probs(m, ProConfig())[0]
        self.assertAlmostEqual(probs["A"], (2 * soft + 3 * sharp) / 5)
        with mock.patch.dict(os.environ, {"LINEMAKER_SHARP_BOOKS": ""}):
            self.assertAlmostEqual(market_probs(m, ProConfig())[0]["A"], (2 * soft + sharp) / 3)
        with mock.patch.dict(os.environ, {"LINEMAKER_SHARP_BOOKS": "Pinnacle:5,LowVig:2"}):
            self.assertAlmostEqual(market_probs(m, ProConfig())[0]["A"], (2 * soft + 5 * sharp) / 7)

    def test_never_bet_at_pinnacle(self):
        cfg = ProConfig()
        self.assertNotIn("Pinnacle", cfg.bet_books or ())


if __name__ == "__main__":
    unittest.main()
