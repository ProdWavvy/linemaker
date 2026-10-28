import os
import os
import unittest
from unittest import mock

from linemaker.live import live_parlay_gates, live_settings
from linemaker.pro import ProConfig, volume_config


class LiveParlayGatesTest(unittest.TestCase):
    def test_defaults(self):
        cfg, _t = live_settings(volume_config(track=False), "2026-10-25")
        self.assertEqual((cfg.min_odds, cfg.max_odds, cfg.min_prob, cfg.max_parlay_legs),
                         (-130, -450, 0.66, 6))

    def test_env_and_non_volume(self):
        with mock.patch.dict(os.environ, {"LINEMAKER_LIVE_ODDS_MAX": "-300",
                                          "LINEMAKER_LIVE_PARLAY_LEGS": "2"}):
            cfg = live_parlay_gates(volume_config())
        self.assertEqual((cfg.max_odds, cfg.max_parlay_legs), (-300, 2))
        plain, _t = live_settings(ProConfig(track=False), "2026-10-25")   # live picks: untouched
        self.assertIsNone(plain.max_parlay_legs)
        self.assertEqual(volume_config().max_odds, -1000)                 # pre-game volume unchanged


class LiveParlaySizeTest(unittest.TestCase):
    def test_sizes_past_three_while_value_holds(self):
        import asyncio
        from linemaker.pro import rows_to_markets, run_pro
        from linemaker.scan_format import volume_messages
        cfg, _t = live_settings(volume_config(track=False), "2026-10-25")
        cfg.min_gap = -1.0
        # 8 games: strong legs (model 80% at -250) plus weak ones (model 66% at -350)
        rows = []
        for i in range(8):
            strong = i < 5
            rows.append({"date": "2026-10-25", "sport": "NBA", "game_id": f"g{i}", "game": f"A{i} @ H{i}",
                         "bet_type": "prop", "subject": f"P{i}", "market": "points", "line": "20.5",
                         "mean": "25.5" if strong else "22.2", "stdev": "6",
                         "books": "FanDuel:-250/+200|DraftKings:-250/+200" if strong else
                                  "FanDuel:-350/+260|DraftKings:-350/+260"})
        res = run_pro(rows_to_markets(rows), "2026-10-25", cfg)
        with mock.patch.dict(os.environ, {"LINEMAKER_SPREAD_PARLAYS": "off"}):   # nested sizes on 8 players
            msgs = volume_messages([("NBA", res, {"profiles": {}, "context": {}, "history": {}})], "2026-10-25", cfg)
        parlays = [p for _e, d in msgs if d and "parlays" in d for p in d["parlays"]]
        sizes = {len(p.legs) for p in parlays}
        self.assertTrue(any(n > 3 for n in sizes), sizes)                       # no hard 3-leg cap
        self.assertTrue(all(len(p.legs) <= 2 or p.prob * p.decimal - 1 >= -0.03 for p in parlays))
