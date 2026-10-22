import os
import unittest
from unittest import mock

from linemaker.pro import ProConfig, rows_to_markets, run_pro


def row(subject, market, line, mean, game="g1", books="FanDuel:-115/-105|DraftKings:-110/-110"):
    return {"date": "2026-10-21", "sport": "NBA", "game_id": game, "game": "A @ H", "bet_type": "prop",
            "subject": subject, "market": market, "line": str(line), "mean": str(mean), "stdev": "5",
            "books": books}


class LadderTest(unittest.TestCase):
    def rows(self):
        # Durant's PRA ladder (four lines, all with an edge), his points, and another player.
        return ([row("Kevin Durant", "PRA", l, 36) for l in (24.5, 25.5, 26.5, 27.5)]
                + [row("Kevin Durant", "points", 22.5, 27), row("Other Guy", "PRA", 25.5, 30, "g2")])

    def test_one_line_per_player_stat(self):
        res = run_pro(rows_to_markets(self.rows()), "2026-10-21", ProConfig(track=False, min_gap=0.0))
        labels = [ev.best.label for ev in res.picks]
        durant_pra = [l for l in labels if l.startswith("Kevin Durant") and "PRA" in l]
        self.assertEqual(len(durant_pra), 1)                       # not 25.5 *and* 26.5
        self.assertTrue(any("Kevin Durant" in l and "points" in l for l in labels))   # other stat stays
        self.assertTrue(any(l.startswith("Other Guy") for l in labels))
        dupes = [r for _l, r in res.passes if r.startswith("similar line already on the board")]
        self.assertEqual(len(dupes), 3)
        # the one kept is the best-ranked line of the ladder
        best = max((ev for ev in res.evaluated if "Durant" in ev.best.label and "PRA" in ev.best.label),
                   key=lambda e: e.best.gap)
        self.assertIn(best.best.label, labels)

    def test_off_switch_and_cap(self):
        with mock.patch.dict(os.environ, {"LINEMAKER_ONE_PER_STAT": "off"}):
            res = run_pro(rows_to_markets(self.rows()), "2026-10-21", ProConfig(track=False, min_gap=0.0))
        self.assertGreaterEqual(sum("Durant" in ev.best.label and "PRA" in ev.best.label for ev in res.picks), 2)
        self.assertEqual(ProConfig().max_picks, 25)
        with mock.patch.dict(os.environ, {"LINEMAKER_MAX_PICKS": "40"}):
            self.assertEqual(ProConfig().max_picks, 40)


if __name__ == "__main__":
    unittest.main()
