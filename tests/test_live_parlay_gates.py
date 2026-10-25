import os
import unittest
from unittest import mock

from linemaker.live import live_parlay_gates, live_settings
from linemaker.pro import ProConfig, volume_config


class LiveParlayGatesTest(unittest.TestCase):
    def test_defaults(self):
        cfg, _t = live_settings(volume_config(track=False), "2026-10-25")
        self.assertEqual((cfg.min_odds, cfg.max_odds, cfg.min_prob, cfg.max_parlay_legs),
                         (-150, -450, 0.72, 3))

    def test_env_and_non_volume(self):
        with mock.patch.dict(os.environ, {"LINEMAKER_LIVE_ODDS_MAX": "-300",
                                          "LINEMAKER_LIVE_PARLAY_LEGS": "2"}):
            cfg = live_parlay_gates(volume_config())
        self.assertEqual((cfg.max_odds, cfg.max_parlay_legs), (-300, 2))
        plain, _t = live_settings(ProConfig(track=False), "2026-10-25")   # live picks: untouched
        self.assertIsNone(plain.max_parlay_legs)
        self.assertEqual(volume_config().max_odds, -1000)                 # pre-game volume unchanged
