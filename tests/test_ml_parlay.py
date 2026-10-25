import os
import tempfile
import unittest
from unittest import mock

from linemaker.chat import game_parlay_types, quick_kind, quick_mode
from linemaker.pro import (ProConfig, ml_parlay_config, ml_parlay_legs, model_weight_for, rows_to_markets,
                           run_pro, volume_config, weight_group)

BOOKS = "FanDuel:{a}/{b}|DraftKings:{a2}/{b2}|BetMGM:{a}/{b}"


def _b(a, b):
    return BOOKS.format(a=a, b=b, a2=a - 5, b2=b - 5)


def slate(favs, spreads=False):
    rows = []
    for i, f in enumerate(favs):
        dog = int(-f * 0.85) if f < -150 else 105
        base = {"date": "2026-10-25", "sport": "NFL", "game_id": f"g{i}", "game": f"A{i} @ H{i}",
                "subject": f"H{i}", "opponent": f"A{i}", "mean": str(3 + i * 1.5), "stdev": "13.5"}
        rows.append(dict(base, bet_type="moneyline", market="moneyline", line="0", books=_b(f, dog)))
        if spreads:
            rows.append(dict(base, bet_type="spread", market="spread", line=str(-(2.5 + i)), books=_b(-110, -110)))
            rows.append(dict(base, bet_type="spread", market="spread", line=str(12.5 + i), books=_b(-900, 550)))
    return rows_to_markets(rows)


class MlParlayTest(unittest.TestCase):
    def test_routing(self):
        for t in ("ML parlay nfl", "nfl moneyline parlay"):
            self.assertEqual((quick_mode(t), quick_kind(t)), ("volume", "games"))
            self.assertEqual(game_parlay_types(t), ["moneyline"])
        self.assertEqual(game_parlay_types("nfl ml and spread parlay"), ["moneyline", "spread"])
        self.assertEqual(game_parlay_types("spread parlay nfl"), ["spread"])
        self.assertEqual(game_parlay_types("ml and totals parlay"), [])        # totals: the normal mix
        self.assertEqual(game_parlay_types("best props parlay"), [])

    def test_favourites_make_the_card(self):
        res = run_pro(slate([-150, -200, -250, -300, -380, -120, -1400]), "2026-10-25", volume_config(track=False))
        self.assertEqual(res.picks, [])                     # the volume gap gate turns them all away
        res = run_pro(slate([-150, -200, -250, -300, -380, -120, -1400]), "2026-10-25",
                      ml_parlay_config(track=False))
        odds = [ev.best.best_odds for ev in res.picks]
        self.assertEqual(len(odds), 4)                      # -120 too short, -1400 too heavy, -150 under 60%
        self.assertTrue(all(-1000 <= o <= -130 for o in odds))
        self.assertTrue(res.ml_only)

    def test_spreads_one_per_game(self):
        res = run_pro(slate([-200, -300], spreads=True), "2026-10-25",
                      ml_parlay_config(("moneyline", "spread"), track=False))
        kinds = sorted(ev.market.bet_type for ev in res.picks)
        self.assertIn("spread", kinds)
        self.assertLessEqual(kinds.count("spread"), 2)      # at most one spread side per game
        res = run_pro(slate([-200, -300], spreads=True), "2026-10-25", ml_parlay_config(("spread",), track=False))
        self.assertTrue(res.picks and all(ev.market.bet_type == "spread" for ev in res.picks))

    def test_logged_as_parlay_legs_not_model_picks(self):
        from linemaker.tracking import Tracker
        with tempfile.TemporaryDirectory() as d:
            t = Tracker(d, "2026-10-25")
            res = run_pro(slate([-150, -200, -250, -300, -380]), "2026-10-25", ml_parlay_config(track=False), None, t)
            self.assertTrue(res.picks)
            for ev in res.picks:
                rec = t.records[ev.market.key]
                self.assertFalse(rec["picked"])
                self.assertEqual(rec["volume_pick"]["side"], ev.best.side)


class GameLineWeightsTest(unittest.TestCase):
    def test_props_and_game_lines_tuned_apart(self):
        self.assertEqual(weight_group("NFL", "receiving yards"), "NFL")
        self.assertEqual(weight_group("NFL", "moneyline"), "NFL games")
        cfg = ProConfig()
        cfg.market_weights, cfg.sport_weights, cfg.model_weight = {}, {"NFL": 0.2, "NFL games": 0.6}, 0.5
        self.assertEqual(model_weight_for("NFL", "receiving yards", cfg, "prop"), 0.2)
        self.assertEqual(model_weight_for("NFL", "moneyline", cfg, "moneyline"), 0.6)
        cfg.sport_weights = {"NFL": 0.2}                    # no game fit yet: not the props weight
        self.assertEqual(model_weight_for("NFL", "spread", cfg, "spread"), 0.5)

    def test_calibration_split(self):
        from linemaker import autotune
        with mock.patch.object(autotune, "load_weights", return_value={"calibration": {"NFL": [0.0, 0.5]}}):
            self.assertNotAlmostEqual(autotune.calibrated(0.9, "NFL", "prop"), 0.9)
            self.assertAlmostEqual(autotune.calibrated(0.9, "NFL", "moneyline"), 0.9)

    def test_tune_groups(self):
        from linemaker.autotune import tune_calibration
        import random
        rng, recs = random.Random(1), []
        for i in range(600):
            for bt, mk in (("prop", "receiving yards"), ("moneyline", "moneyline")):
                p = rng.uniform(0.55, 0.95)                 # overconfident: truth is halfway to 50%
                won = rng.random() < 0.5 + (p - 0.5) / 2
                recs.append({"sport": "NFL", "game_id": f"g{i % 60}", "subject": f"s{i}", "market": mk,
                             "inputs": {"bet_type": bt}, "result": {"winner": "A" if won else "B"},
                             "sides": [{"side": "A", "blended": p}, {"side": "B", "blended": 1 - p}]})
        out, _notes = tune_calibration(recs)
        self.assertEqual(set(out), {"NFL", "NFL games"})   # one fit each, never mixed
