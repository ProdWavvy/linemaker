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
        res = run_pro(rows_to_markets(self.rows()), "2026-10-21", ProConfig(track=False, min_gap=0.0, max_per_player=0))
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
            res = run_pro(rows_to_markets(self.rows()), "2026-10-21", ProConfig(track=False, min_gap=0.0, max_per_player=0))
        self.assertGreaterEqual(sum("Durant" in ev.best.label and "PRA" in ev.best.label for ev in res.picks), 2)
        self.assertEqual(ProConfig().max_picks, 25)
        with mock.patch.dict(os.environ, {"LINEMAKER_MAX_PICKS": "40"}):
            self.assertEqual(ProConfig().max_picks, 40)


if __name__ == "__main__":
    unittest.main()


class TrackEverythingTest(unittest.TestCase):
    def test_unbettable_props_still_tracked(self):
        import json
        import tempfile
        from linemaker.tracking import Tracker
        rows = [row("Bettable Guy", "points", 20.5, 25),
                row("Other Books Only", "points", 20.5, 25, "g2", books="BetMGM:-110/-110|Caesars:-115/-105")]
        with tempfile.TemporaryDirectory() as d:
            res = run_pro(rows_to_markets(rows), "2026-10-21",
                          ProConfig(min_gap=0.0, bet_books=("FanDuel", "DraftKings")),
                          tracker=Tracker(d, "2026-10-21"))
            if True:
                from linemaker.tracking import load_day
                recs = {r["subject"]: r for r in load_day(d, "2026-10-21").values()}
        self.assertEqual(set(recs), {"Bettable Guy", "Other Books Only"})
        self.assertFalse(recs["Other Books Only"]["bettable"])
        self.assertIsNotNone(recs["Other Books Only"]["market_prob_a"])   # usable for calibration
        self.assertNotIn("bettable", recs["Bettable Guy"])
        self.assertEqual([ev.market.subject for ev in res.picks], ["Bettable Guy"])   # never picked


class TrackGameLinesTest(unittest.TestCase):
    def test_logs_without_picks(self):
        import json
        import tempfile
        from linemaker import chat, game_model
        from tests.test_game_model import LAST, NOW
        from tests.test_oddsapi import FakeAPI
        tables = {2027: game_model.parse_standings(NOW), 2026: game_model.parse_standings(LAST)}
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"LINEMAKER_TRACKING": "on", "LINEMAKER_TRACKING_DIR": d,
                                             "LINEMAKER_KALSHI_WEIGHT": "0"}), \
                mock.patch.object(chat, "OddsAPI", lambda: FakeAPI()), \
                mock.patch.object(game_model, "standings", lambda s, y: tables[y]), \
                mock.patch.object(game_model, "played_yesterday", lambda s, d: set()):
            n = chat.track_game_lines("NBA", "2026-10-21")
            if True:
                from linemaker.tracking import load_day
                recs = load_day(d, "2026-10-21").values()
        self.assertGreater(n, 0)
        self.assertEqual({r["inputs"]["bet_type"] for r in recs}, {"moneyline", "spread", "total"})
        self.assertFalse(any(r["picked"] for r in recs))      # logged, never counted as picks
