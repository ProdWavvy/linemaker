import json
import os
import random
import tempfile
import unittest
from unittest import mock

from linemaker import autotune
from linemaker.pro import ProConfig


def records(n, sport="NBA", market="points", model_skill=0.9, seed=1):
    """Markets where the truth is mostly the model's view (skill near 1) or the market's (near 0)."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        model, mkt = rng.uniform(0.3, 0.8), rng.uniform(0.3, 0.8)
        truth = model_skill * model + (1 - model_skill) * mkt
        out.append({"sport": sport, "market": market, "game_id": f"g{seed}-{i // 40}", "subject": f"p{i}",
                    "model_prob_a": model, "market_prob_a": mkt,
                    "inputs": {"bet_type": "prop"},
                    "result": {"winner": "A" if rng.random() < truth else "B"}})
    return out


class TuneTest(unittest.TestCase):
    def test_waits_for_enough_results(self):
        new, changes = autotune.tune(records(50), {})
        self.assertEqual(new["model_weight"], 0.5)
        self.assertIn("Waiting for 200", changes[0])
        ladder = records(300)
        for r in ladder:
            r["subject"], r["game_id"] = "same player", "g1"   # one stat's alt ladder = one outcome
        self.assertIn("have 1;", autotune.tune(ladder, {})[1][0])

    def test_two_nights_of_noise_dont_move_it(self):
        # ~2 nights: 500 outcomes in 22 games, where the truth sits halfway (no real signal).
        recs = records(500, model_skill=0.5, seed=7)
        for i, r in enumerate(recs):
            r["game_id"] = f"night{i % 22}"
        new, changes = autotune.tune(recs, {"model_weight": 0.5})
        self.assertEqual(new["model_weight"], 0.5)

    def test_moves_toward_best_fit_in_capped_steps(self):
        recs = records(3000, model_skill=0.95)
        new, changes = autotune.tune(recs, {"model_weight": 0.5})
        self.assertAlmostEqual(new["model_weight"], 0.6)      # capped at +0.10 per run
        for _ in range(5):
            new, _c = autotune.tune(recs, new)
        self.assertGreaterEqual(new["model_weight"], 0.8)   # stops once more weight is noise
        self.assertLessEqual(new["model_weight"], autotune.HIGH)
        self.assertTrue(new["history"])

    def test_market_weight_shrinks_toward_overall(self):
        recs = records(2000, model_skill=0.5) + records(200, market="threes", model_skill=0.0, seed=2)
        state = {"model_weight": 0.5}
        for _ in range(6):
            state, _c = autotune.tune(recs, state)
        threes = state["markets"].get("NBA threes")
        self.assertIsNotNone(threes)
        self.assertLess(threes, state["model_weight"])
        self.assertGreater(threes, 0.1)                       # pulled back toward overall

    def test_engine_reads_saved_weights(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "weights.json")
            with open(path, "w") as f:
                json.dump({"model_weight": 0.62, "fresh_weight": 0.7, "markets": {"NBA threes": 0.3}}, f)
            with mock.patch.dict(os.environ, {"LINEMAKER_WEIGHTS_FILE": path, "LINEMAKER_MODEL_WEIGHT": "0.5"}):
                from linemaker.pro import model_weight_for
                cfg = ProConfig()
                self.assertEqual((cfg.model_weight, cfg.model_weight_fresh), (0.62, 0.7))
                self.assertEqual(model_weight_for("NBA", "threes", cfg), 0.3)    # old file: pre-game props
                from linemaker.live import live_settings
                live, _t = live_settings(ProConfig(), "2026-10-21")
                self.assertEqual(model_weight_for("NBA", "threes", live), 0.35)  # pre-game tuning stays pre-game
                with mock.patch.dict(os.environ, {"LINEMAKER_AUTOTUNE": "off"}):
                    self.assertEqual(ProConfig().model_weight, 0.5)

    def test_run_writes_file_and_tracking_ignores_it(self):
        from linemaker.tracking import load_records
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "2026-10-20.json"), "w") as f:
                json.dump({f"k{i}": r for i, r in enumerate(records(400, model_skill=0.95))}, f)
            with mock.patch.dict(os.environ, {"LINEMAKER_WEIGHTS_FILE": "", "LINEMAKER_TRACKING_DIR": tmp}):
                lines = autotune.run_autotune(tmp, "2026-10-21")
                self.assertTrue(os.path.exists(os.path.join(tmp, "weights.json")))
                self.assertIn("Overall weight 0.50", lines[0])
                self.assertEqual(len(load_records(tmp)), 400)   # weights.json isn't a day of records


if __name__ == "__main__":
    unittest.main()


class HierarchyTest(unittest.TestCase):
    def test_sport_gets_its_own_weight_and_markets_shrink_to_it(self):
        recs = (records(3000, sport="NBA", market="points", model_skill=0.1, seed=3)
                + records(3000, sport="NFL", market="receiving yards", model_skill=0.95, seed=4))
        with mock.patch.dict(os.environ, {"LINEMAKER_AUTOTUNE_STEP": "1.0"}):
            new, changes = autotune.tune(recs, {})
        self.assertLess(new["sports"]["NBA"], new["model_weight"])     # market knows more in NBA
        self.assertGreater(new["sports"]["NFL"], new["model_weight"])  # the model knows more in NFL
        self.assertTrue(any(c.startswith("NBA:") for c in changes))
        # a market with one stat per sport sits on its sport's weight: no separate entry
        self.assertNotIn("NBA points", new["markets"])

    def test_thin_sport_stays_near_its_start(self):
        recs = records(3000, model_skill=0.5, seed=5) + records(80, sport="UFC", market="moneyline",
                                                               model_skill=1.0, seed=6)
        with mock.patch.dict(os.environ, {"LINEMAKER_AUTOTUNE_STEP": "1.0"}):
            new, _ = autotune.tune(recs, {})
        ufc = new["sports"].get("UFC", 0.35)
        self.assertLess(ufc, 0.35 + 80 / 230 * 0.65 + 1e-9)   # credibility 80/(80+150) at most


class WeightLookupTest(unittest.TestCase):
    def test_most_specific_wins(self):
        from linemaker.pro import model_weight_for
        cfg = ProConfig(track=False)
        cfg.model_weight = 0.5
        cfg.segment_weights = {"pre props": {"sports": {"NBA": 0.4}, "markets": {"NBA points": 0.3}}}
        self.assertEqual(model_weight_for("NBA", "points", cfg), 0.3)
        self.assertEqual(model_weight_for("NBA", "assists", cfg), 0.4)
        self.assertEqual(model_weight_for("NHL", "shots on goal", cfg), 0.5)
        self.assertEqual(model_weight_for("UFC", "moneyline", cfg), 0.35)
        with mock.patch.dict(os.environ, {"LINEMAKER_NHL_MODEL_WEIGHT": "0.6"}):
            self.assertEqual(model_weight_for("NHL", "shots on goal", cfg), 0.6)


class SportSettingsTest(unittest.TestCase):
    def test_per_sport_gate(self):
        from linemaker.pro import rows_to_markets, run_pro
        row = {"date": "2026-10-21", "game_id": "g", "game": "A @ H", "bet_type": "prop",
               "market": "points", "line": "20.5", "mean": "19.6",
               "books": "FanDuel:-110/-110|DraftKings:-110/-110"}
        rows = [dict(row, sport="NBA", subject="Nba Guy"), dict(row, sport="WNBA", subject="Wnba Guy", game_id="g2")]
        with mock.patch.dict(os.environ, {"LINEMAKER_NBA_MIN_GAP": "0.10", "LINEMAKER_MIN_GAP": ""}):
            res = run_pro(rows_to_markets(rows), "2026-10-21", ProConfig(n_sims=4000, track=False))
        self.assertEqual({e.market.sport for e in res.picks}, {"WNBA"})
        self.assertTrue(any(l.startswith("Nba Guy") and "no gap" in r for l, r in res.passes))


class BiasTest(unittest.TestCase):
    def _recs(self, n, ratio, seed=1, mean=20.0, bias_applied=1.0):
        rng = random.Random(seed)
        out = []
        for i in range(n):
            actual = max(0.0, rng.gauss(mean * ratio, 6))
            out.append({"sport": "NBA", "market": "points", "game_id": f"g{i // 8}", "subject": f"p{i}",
                        "inputs": {"bet_type": "prop", "line": 19.5, "mean": mean * bias_applied,
                                   "mean_bias": bias_applied},
                        "result": {"actual": actual, "winner": "A"}})
        return out

    def test_high_projections_get_trimmed(self):
        bias, changes = autotune.tune_bias(self._recs(1500, 0.92), {})
        self.assertAlmostEqual(bias["NBA points"], 0.96)          # one capped step toward ~0.92
        self.assertIn("NBA points projections ×1.00 → ×0.96", changes[0])
        bias2, _ = autotune.tune_bias(self._recs(1500, 0.92), {"bias": bias})
        self.assertAlmostEqual(bias2["NBA points"], 1 + 1500 / 1650 * -0.08, delta=0.006)   # shrunk toward 1

    def test_noise_doesnt_move_it(self):
        bias, changes = autotune.tune_bias(self._recs(200, 1.0, seed=3), {})
        self.assertEqual((bias, changes), ({}, []))

    def test_alt_lines_count_once_and_raw_mean_is_used(self):
        recs = self._recs(1500, 0.92, bias_applied=0.95)            # already corrected by 0.95
        ladder = [dict(r, inputs=dict(r["inputs"], line=29.5)) for r in recs]
        bias, _ = autotune.tune_bias(recs + ladder, {"bias": {"NBA points": 0.95}})
        self.assertAlmostEqual(bias["NBA points"], 1 + 1500 / 1650 * -0.08, delta=0.006)   # target from raw means


class CalibrationTest(unittest.TestCase):
    def _recs(self, n, truth, seed=1):
        rng = random.Random(seed)
        out = []
        for i in range(n):
            p = rng.uniform(0.55, 0.97)
            out.append({"sport": "NBA", "market": "points", "game_id": f"g{i // 20}", "subject": f"p{i}",
                        "sides": [{"side": "A", "blended": p}, {"side": "B", "blended": 1 - p}],
                        "result": {"winner": "A" if rng.random() < truth(p) else "B"}})
        return out

    def test_overconfident_extremes_get_pulled_in(self):
        over = lambda p: 0.5 + (p - 0.5) * 0.75          # a "90%" really hits 80%
        cal, notes = autotune.tune_calibration(self._recs(4000, over))
        a, b, n = cal["NBA"]
        self.assertLess(autotune.calibrate_p(0.9, a, b), 0.87)
        self.assertIn("NBA hit chances recalibrated", notes[0])

    def test_honest_chances_left_alone(self):
        cal, _ = autotune.tune_calibration(self._recs(4000, lambda p: p, seed=2))
        self.assertEqual(cal, {})


class VolumeSelectionTest(unittest.TestCase):
    def test_score_prefers_the_line_that_pays_for_its_risk_and_one_leg_per_player(self):
        from linemaker.pro import rows_to_markets, run_pro, volume_config
        base = {"date": "2026-10-21", "sport": "NBA", "game_id": "g", "game": "A @ H", "bet_type": "prop",
                "subject": "Star", "mean": "28", "stdev": "4"}
        rows = [dict(base, market="points", line="20.5", books="FanDuel:-900/+600|DraftKings:-900/+600"),
                dict(base, market="points", line="22.5", books="FanDuel:-420/+320|DraftKings:-420/+320"),
                dict(base, market="PRA", line="30.5", mean="40", books="FanDuel:-500/+380|DraftKings:-500/+380")]
        with mock.patch.dict(os.environ, {"LINEMAKER_TRACKING": "off"}):
            res = run_pro(rows_to_markets(rows), "2026-10-21", volume_config(track=False, n_sims=4000))
        self.assertEqual(len(res.picks), 1)                                   # one leg for Star
        self.assertTrue(any("same player already" in r for _l, r in res.passes))
