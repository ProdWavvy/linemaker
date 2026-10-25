import os
import tempfile
import unittest
from unittest import mock

from linemaker.chat import game_parlay_types, quick_kind, quick_mode
from linemaker.pro import (ProConfig, ml_parlay_config, ml_parlay_legs, model_weight_for, rows_to_markets,
                           run_pro, volume_config)

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


class SegmentWeightsTest(unittest.TestCase):
    def test_each_segment_uses_only_its_own_weights(self):
        cfg = ProConfig()
        cfg.model_weight = 0.5
        cfg.segment_weights = {"pre props": {"weight": 0.45, "sports": {"NFL": 0.2}},
                               "pre games": {"weight": 0.55, "sports": {"NFL": 0.6}},
                               "live props": {"weight": 0.3, "sports": {"NBA": 0.25}},
                               "live games": {"weight": 0.4}}
        self.assertEqual(model_weight_for("NFL", "receiving yards", cfg, "prop"), 0.2)
        self.assertEqual(model_weight_for("NFL", "moneyline", cfg, "moneyline"), 0.6)
        self.assertEqual(model_weight_for("NHL", "spread", cfg, "spread"), 0.55)   # games base, not props
        cfg.phase = "live"
        self.assertEqual(model_weight_for("NBA", "points", cfg, "prop"), 0.25)
        self.assertEqual(model_weight_for("NFL", "receiving yards", cfg, "prop"), 0.3)
        self.assertEqual(model_weight_for("NBA", "moneyline", cfg, "moneyline"), 0.4)

    def test_calibration_per_segment(self):
        from linemaker import autotune
        segs = {"segments": {"pre props": {"calibration": {"NFL": [0.0, 0.5]}},
                             "live games": {"calibration": {"NFL": [0.0, 2.0]}}}}
        with mock.patch.object(autotune, "load_weights", return_value=segs):
            self.assertLess(autotune.calibrated(0.9, "NFL", "prop"), 0.9)
            self.assertAlmostEqual(autotune.calibrated(0.9, "NFL", "moneyline"), 0.9)          # pre games: none
            self.assertGreater(autotune.calibrated(0.9, "NFL", "moneyline", "live"), 0.9)
            self.assertAlmostEqual(autotune.calibrated(0.9, "NFL", "prop", "live"), 0.9)

    def test_legacy_file_maps_into_segments(self):
        from linemaker import autotune
        old = {"model_weight": 0.6, "live_weight": 0.3, "sports": {"NBA": 0.4, "NFL games": 0.7},
               "markets": {"NBA points": 0.45, "NFL moneyline": 0.65}, "calibration": {"NBA": [0, 1], "NFL games": [0, 1]}}
        segs = autotune._legacy_segments(old)
        self.assertEqual(segs["pre props"]["sports"], {"NBA": 0.4})
        self.assertEqual(segs["pre games"]["sports"], {"NFL": 0.7})
        self.assertEqual(segs["pre games"]["markets"], {"NFL moneyline": 0.65})
        self.assertEqual(segs["live games"]["weight"], 0.3)

    def test_autotune_fits_segments_apart(self):
        import json
        from linemaker import autotune
        from tests.test_autotune import records
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"LINEMAKER_WEIGHTS_FILE": "", "LINEMAKER_TRACKING_DIR": d,
                                             "LINEMAKER_AUTOTUNE_STEP": "1.0"}):
            props = records(3000, sport="NFL", market="receiving yards", model_skill=0.95, seed=11)
            games = records(3000, sport="NFL", market="moneyline", model_skill=0.05, seed=12)
            for r in games:
                r["inputs"] = {"bet_type": "moneyline"}
            for seg, recs in (("NFL-props", props), ("NFL-games", games)):
                os.makedirs(os.path.join(d, seg))
                with open(os.path.join(d, seg, "2026-10-20.json"), "w") as f:
                    json.dump({f"{seg}{i}": r for i, r in enumerate(recs)}, f)
            autotune.run_autotune(d, "2026-10-21")
            segs = autotune.segment_weights()
        self.assertGreater(segs["pre props"]["weight"], 0.7)    # the model reads NFL props well
        self.assertLess(segs["pre games"]["weight"], 0.3)       # the market reads NFL lines better
        self.assertEqual(segs["live props"]["graded"], 0)


class SegmentTrackingTest(unittest.TestCase):
    def test_files_split_by_phase_league_and_kind(self):
        from linemaker.live import live_settings
        from linemaker.tracking import Tracker, load_day, load_records
        rows = []
        for sport in ("NBA", "NFL"):
            rows.append({"date": "2026-10-25", "sport": sport, "game_id": f"{sport}1", "game": "A @ H",
                         "bet_type": "prop", "subject": f"{sport} Guy", "market": "points", "line": "20.5",
                         "mean": "24", "stdev": "5", "books": _b(-110, -110)})
            rows.append({"date": "2026-10-25", "sport": sport, "game_id": f"{sport}1", "game": "A @ H",
                         "bet_type": "moneyline", "subject": "H", "opponent": "A", "market": "moneyline",
                         "line": "0", "mean": "3", "stdev": "13", "books": _b(-150, 130)})
        with tempfile.TemporaryDirectory() as d:
            run_pro(rows_to_markets(rows), "2026-10-25", ProConfig(), None, Tracker(d, "2026-10-25"))
            live_dir = os.path.join(d, "live")
            cfg, _t = live_settings(ProConfig(), "2026-10-25")
            res = run_pro(rows_to_markets(rows), "2026-10-25", cfg, None, Tracker(live_dir, "2026-10-25"))
            self.assertTrue(all(ev.phase == "live" for ev in res.evaluated))
            self.assertEqual(sorted(os.listdir(d)), ["NBA-games", "NBA-props", "NFL-games", "NFL-props", "live"])
            self.assertEqual(sorted(os.listdir(live_dir)), ["NBA-games", "NBA-props", "NFL-games", "NFL-props"])
            self.assertEqual(len(load_day(d, "2026-10-25")), 4)
            self.assertEqual({r["segment"] for r in load_records(d)}, {"NBA-games", "NBA-props", "NFL-games", "NFL-props"})
            self.assertEqual(len(load_records(d, segments={"NFL-games"})), 1)

    def test_old_mixed_file_is_split(self):
        import json
        from linemaker.tracking import load_day
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "2026-10-24.json"), "w") as f:
                json.dump({"a": {"sport": "NHL", "inputs": {"bet_type": "prop"}},
                           "b": {"sport": "NHL", "inputs": {"bet_type": "spread"}}}, f)
            self.assertEqual(set(load_day(d, "2026-10-24")), {"a", "b"})
            self.assertTrue(os.path.exists(os.path.join(d, "NHL-props", "2026-10-24.json")))
            self.assertTrue(os.path.exists(os.path.join(d, "NHL-games", "2026-10-24.json")))
            self.assertTrue(os.path.exists(os.path.join(d, "2026-10-24.json.migrated")))

    def test_live_bet_leg_settles_from_live_file(self):
        from linemaker.bets import leg_from_eval
        from linemaker.live import live_settings
        cfg, _t = live_settings(ProConfig(track=False), "2026-10-25")
        res = run_pro(slate([-300]), "2026-10-25", cfg)
        self.assertTrue(leg_from_eval(res.evaluated[0])["live"])
        res = run_pro(slate([-300]), "2026-10-25", ProConfig(track=False))
        self.assertFalse(leg_from_eval(res.evaluated[0])["live"])


class LiveGameParlayTest(unittest.TestCase):
    def test_routing(self):
        for t, kind, types in (("live ml parlay nba", "live", ["moneyline"]),
                               ("live ml and spread parlay nfl", "live", ["moneyline", "spread"]),
                               ("nhl live puck line parlay", "live", ["spread"]),
                               ("mlb ml and run line parlay", "games", ["moneyline", "spread"]),
                               ("nfl ats parlay", "games", ["spread"])):
            self.assertEqual((quick_kind(t), game_parlay_types(t)), (kind, types), t)

    def test_live_gates_and_live_tracking(self):
        from linemaker.live import live_settings
        from linemaker.tracking import Tracker, load_day
        with tempfile.TemporaryDirectory() as d:
            cfg, _t = live_settings(ml_parlay_config(("moneyline", "spread")), "2026-10-25")
            self.assertEqual((cfg.phase, cfg.min_odds, cfg.max_odds, cfg.max_parlay_legs), ("live", -130, -450, 6))
            live = os.path.join(d, "live")
            res = run_pro(slate([-150, -200, -250, -300, -380, -600], spreads=True), "2026-10-25", cfg, None,
                          Tracker(live, "2026-10-25"))
            self.assertTrue(res.picks)
            self.assertTrue(all(-450 <= ev.best.best_odds <= -130 for ev in res.picks))
            self.assertTrue(all(ev.best.blended >= 0.66 for ev in res.picks))
            recs = load_day(live, "2026-10-25")
            self.assertTrue(all(recs[ev.market.key].get("volume_pick") for ev in res.picks))
            self.assertFalse(os.path.exists(os.path.join(d, "NFL-games")))     # nothing pre-game
