import math
import os
import tempfile
import unittest

from linemaker.devig import hold, no_vig, overround
from linemaker.freeze import Freezer, grade
from linemaker.odds import fair_odds, implied_probability
from linemaker.pro import ProConfig, load_pro_markets, run_pro
from linemaker.sim import simulate

HERE = os.path.dirname(__file__)
SLATE = os.path.join(HERE, "..", "data", "sample_pro_slate.csv")
RESULTS = os.path.join(HERE, "..", "data", "sample_results.csv")


class DevigTest(unittest.TestCase):
    def test_handoff_example(self):
        # Over -115 / Under -105: 53.5% + 51.2% = 104.7%, 4.5% hold, 51.1% no-vig over.
        self.assertAlmostEqual(implied_probability(-115), 0.535, places=3)
        self.assertAlmostEqual(implied_probability(-105), 0.512, places=3)
        self.assertAlmostEqual(overround(-115, -105), 0.047, places=3)
        self.assertAlmostEqual(hold(-115, -105), 0.045, places=3)
        over, under = no_vig(-115, -105)
        self.assertAlmostEqual(over, 0.511, places=3)
        self.assertAlmostEqual(over + under, 1.0)

    def test_fair_odds_58(self):
        self.assertEqual(fair_odds(0.58), -138)


class SimTest(unittest.TestCase):
    def test_poisson_matches_closed_form(self):
        r = simulate(0.55, 0.5, "poisson", math.sqrt(0.55), n=50000, seed_key="td")
        self.assertAlmostEqual(r.p_over, 1 - math.exp(-0.55), delta=0.01)

    def test_right_skew_mean_above_median(self):
        r = simulate(5.6, 5.5, "negbin", 3.0, n=20000, seed_key="k")
        self.assertLess(r.median, r.mean)
        self.assertLess(r.p_over, 0.5)   # mean clears the line, the median does not

    def test_integer_line_push(self):
        r = simulate(3.0, 3.0, "poisson", math.sqrt(3), n=20000, seed_key="p")
        self.assertGreater(r.p_push, 0.15)
        self.assertAlmostEqual(r.p_over + r.p_push + r.p_under, 1.0)

    def test_deterministic(self):
        a = simulate(70, 60.5, "gamma", 35, n=5000, seed_key="x")
        b = simulate(70, 60.5, "gamma", 35, n=5000, seed_key="x")
        self.assertEqual(a.p_over, b.p_over)


class ProPipelineTest(unittest.TestCase):
    def test_slate_and_freeze_and_grade(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = ProConfig(n_sims=5000, over_min_gap=None)
            res = run_pro(load_pro_markets(SLATE), "2026-10-21", cfg, Freezer(d, "2026-10-21"))
            self.assertTrue(res.picks)
            self.assertTrue(all(e.best.gap > 0 for e in res.picks))
            gaps = [e.best.gap for e in res.picks]
            self.assertEqual(gaps, sorted(gaps, reverse=True))
            self.assertIn("NFL1", res.correlated)
            self.assertTrue(any("slate" in r for _, r in res.passes))

            # Unconfirmed lineup is not frozen; confirmed ones are.
            fz = Freezer(d, "2026-10-21")
            self.assertFalse(any("Guard Example" in k for k in fz.records))
            self.assertTrue(any("RB Example" in k for k in fz.records))

            # Changing a projection after freeze does not change the frozen number.
            markets = load_pro_markets(SLATE)
            rb = next(m for m in markets if m.subject == "RB Example")
            before = fz.records[rb.key]["sim"]["p_over"]
            rb.mean = 40
            res2 = run_pro(markets, "2026-10-21", cfg, fz)
            self.assertEqual(Freezer(d, "2026-10-21").records[rb.key]["sim"]["p_over"], before)
            self.assertTrue(any("changed after freeze" in r for _, r in res2.passes))

            report = grade(os.path.join(d, "2026-10-21.json"), RESULTS)
            self.assertIn("Bets graded", report)
            self.assertIn("Brier", report)


if __name__ == "__main__":
    unittest.main()


class ProjectionTest(unittest.TestCase):
    def test_opportunity_x_rate_with_factors(self):
        from linemaker.projection import ProjectionConfig, ProjectionInputs, build_projection
        p = build_projection(ProjectionInputs(opp_season=10, rate_season=5.0, matchup_factor=1.10),
                             ProjectionConfig(factor_strength=0.5))
        self.assertAlmostEqual(p.mean, 10 * 5.0 * 1.05)

    def test_recent_role_moves_fast_efficiency_slow(self):
        from linemaker.projection import ProjectionInputs, build_projection
        p = build_projection(ProjectionInputs(opp_season=10, opp_recent=20, opp_recent_games=5,
                                              rate_season=1.0, rate_recent=2.0, rate_recent_games=5))
        self.assertAlmostEqual(p.opportunity, 15.0)      # k=5: half weight after 5 games
        self.assertLess(p.rate, 1.2)                     # k=25: efficiency barely moves

    def test_quality_signal_regresses_efficiency(self):
        from linemaker.projection import ProjectionInputs, build_projection
        p = build_projection(ProjectionInputs(season_games=10, opp_season=10, rate_season=0.30,
                                              rate_expected=0.20))
        self.assertAlmostEqual(p.rate, 0.25)

    def test_factor_clamped(self):
        from linemaker.projection import ProjectionInputs, build_projection
        p = build_projection(ProjectionInputs(opp_season=10, rate_season=1.0, venue_factor=3.0))
        self.assertAlmostEqual(p.mean, 12.0)

    def test_requires_inputs(self):
        from linemaker.projection import ProjectionInputs, build_projection
        with self.assertRaises(ValueError):
            build_projection(ProjectionInputs(opp_season=10))


class BooksTest(unittest.TestCase):
    def test_best_price_and_consensus(self):
        from linemaker.pro import best_price, parse_books
        books = parse_books("DraftKings:-115/-105|FanDuel:-110/-112|Caesars:+130/")
        self.assertEqual(books[2], ("Caesars", [130, None]))
        self.assertEqual(best_price(books, 0), (130, "Caesars"))
        self.assertEqual(best_price(books, 1), (-105, "DraftKings"))

    def test_soft_market_tags(self):
        from linemaker.projection import soft_market_note
        self.assertIn("QB", soft_market_note("rushing yards", "QB"))
        self.assertIsNone(soft_market_note("rushing yards", "RB"))
        self.assertIsNotNone(soft_market_note("anytime td", ""))


class FormulaSheetTest(unittest.TestCase):
    """Every worked number from the formula write-up."""

    def test_implied_anchors(self):
        for odds, want in ((-110, 0.5238), (-300, 0.75), (-200, 0.6667), (200, 0.3333), (150, 0.40)):
            self.assertAlmostEqual(implied_probability(odds), want, places=4)

    def test_overround(self):
        from linemaker.odds import parse_odds
        self.assertAlmostEqual(overround(-110, -110), 0.0476, places=4)
        self.assertAlmostEqual(overround(*[parse_odds(x) for x in ("2.00", "3.50", "4.00")]),
                               0.0357, places=4)
        self.assertAlmostEqual(overround(120, 250, 180), 0.0974, places=4)  # sheet rounds to 9.73

    def test_devig(self):
        self.assertAlmostEqual(no_vig(-110, -110)[0], 0.50)
        # 57.8 / 105.1 = 55.0%
        self.assertAlmostEqual(0.578 / (0.578 + 0.231 + 0.242), 0.55, places=3)
        self.assertAlmostEqual(1 / 0.55, 1.82, places=2)

    def test_ev(self):
        from linemaker.odds import expected_value
        self.assertAlmostEqual(expected_value(0.38, 200), 14.0)
        self.assertAlmostEqual(expected_value(0.38, 150), -5.0)

    def test_prob_to_odds(self):
        self.assertEqual(fair_odds(0.58), -138)
        self.assertEqual(fair_odds(0.60), -150)
        self.assertEqual(fair_odds(0.40), 150)

    def test_shin_shifts_margin_to_longshot(self):
        prop = no_vig(-400, 300)
        shin = no_vig(-400, 300, method="shin")
        self.assertAlmostEqual(sum(shin), 1.0)
        self.assertGreater(shin[0], prop[0])     # favorite gets more, longshot less
        self.assertAlmostEqual(no_vig(-110, -110, method="shin")[0], 0.5)

    def test_decimal_parsing(self):
        from linemaker.odds import parse_odds
        self.assertEqual(parse_odds("2.00"), 100)
        self.assertEqual(parse_odds("3.50"), 250)
        self.assertEqual(parse_odds("1.91"), -110)
        self.assertEqual(parse_odds("-110"), -110)
        self.assertEqual(parse_odds("150"), 150)


class ThreeWayTest(unittest.TestCase):
    def test_1x2_sim_and_grade(self):
        from linemaker.sim import simulate_1x2
        r = simulate_1x2(1.5, 1.5, n=20000, seed_key="x")
        self.assertAlmostEqual(r.p_over, r.p_under, delta=0.02)
        self.assertAlmostEqual(r.p_over + r.p_push + r.p_under, 1.0)
        with tempfile.TemporaryDirectory() as d:
            res = run_pro(load_pro_markets(SLATE), "2026-10-21", ProConfig(n_sims=5000),
                          Freezer(d, "2026-10-21"))
            ucl = next(e for e in res.picks if e.market.bet_type == "1x2")
            self.assertEqual(len(ucl.sides), 3)
            self.assertAlmostEqual(sum(s.market_prob for s in ucl.sides), 1.0)
            report = grade(os.path.join(d, "2026-10-21.json"), RESULTS)
            self.assertIn("Example FC win", report)

    def test_market_caught_up_passes(self):
        # A bet needs model > no-vig market, not just blended > price.
        with tempfile.TemporaryDirectory() as d:
            res = run_pro(load_pro_markets(SLATE), "2026-10-21", ProConfig(n_sims=5000),
                          Freezer(d, "2026-10-21"))
            self.assertTrue(all(e.best.model_edge > 0 for e in res.picks))


class BetBooksTest(unittest.TestCase):
    def test_only_bettable_books_are_shopped(self):
        from linemaker.pro import best_price, parse_books
        books = parse_books("Bovada:+160/-190|DraftKings:+150/-180|FanDuel:+145/-175")
        self.assertEqual(best_price(books, 0), (160, "Bovada"))
        self.assertEqual(best_price(books, 0, ("FanDuel", "DraftKings")), (150, "DraftKings"))
        self.assertEqual(best_price(books, 1, ("fanduel", "draft kings")), (-175, "FanDuel"))

    def test_consensus_uses_all_books_but_bets_only_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = ProConfig(n_sims=3000, bet_books=("FanDuel", "DraftKings"))
            res = run_pro(load_pro_markets(SLATE), "2026-10-21", cfg, Freezer(d, "2026-10-21"))
        for ev in res.picks:
            self.assertIn(ev.best.best_book, ("FanDuel", "DraftKings"))
        self.assertTrue(res.picks)

    def test_market_with_no_bettable_book_passes(self):
        from linemaker.pro import rows_to_markets
        m = rows_to_markets([{"date": "2026-10-21", "sport": "NBA", "game_id": "g", "game": "A @ B",
                              "lineup_confirmed": "true", "bet_type": "prop", "subject": "P",
                              "market": "points", "line": "20.5", "mean": "30",
                              "books": "BetRivers:-110/-110|Bovada:-105/-115"}])
        res = run_pro(m, "2026-10-21", ProConfig(n_sims=2000, bet_books=("FanDuel", "DraftKings")))
        self.assertEqual(res.picks, [])
        self.assertIn("no bettable price at FanDuel/DraftKings", res.passes[0][1])


class TrackingTest(unittest.TestCase):
    def test_tracks_every_market_and_calibrates(self):
        import json
        import random
        from linemaker.tracking import Tracker, apply_results, calibrate, load_records
        with tempfile.TemporaryDirectory() as d:
            res = run_pro(load_pro_markets(SLATE), "2026-10-21", ProConfig(n_sims=2000),
                          tracker=Tracker(d, "2026-10-21"))
            if True:
                from linemaker.tracking import load_day
                recs = load_day(d, "2026-10-21")
            self.assertEqual(len(recs), len(res.evaluated))          # all markets, not just picks
            self.assertGreater(len(recs), len(res.picks))
            self.assertEqual(sum(r["picked"] for r in recs.values()), len(res.picks))
            rb = next(r for r in recs.values() if r["subject"] == "RB Example")
            self.assertTrue(rb["projection_steps"])                  # Step/Value kept in the back end
            self.assertIn("DraftKings", rb["books"]["A"])
            n = __import__("linemaker.tracking", fromlist=["x"]).apply_results_day(d, "2026-10-21", [
                {"game_id": "NFL1", "subject": "RB Example", "market": "rushing yards",
                 "line": "60.5", "actual": "74"}])
            self.assertEqual(n, 1)
            # Synthetic history where the model is right more often than the market.
            rng = random.Random(1)
            fake = {}
            for i in range(300):
                truth = rng.random()
                fake[f"k{i}"] = {"sport": "NBA", "market": "points", "inputs": {"bet_type": "prop"},
                                 "model_prob_a": min(max(truth + rng.gauss(0, 0.05), 0.02), 0.98),
                                 "market_prob_a": 0.5, "model_weight": 0.5, "picked": False,
                                 "best_side": "A", "sides": [],
                                 "result": {"winner": "A" if rng.random() < truth else "B"}}
            with open(os.path.join(d, "2026-10-20.json"), "w") as f:
                json.dump(fake, f)
            report = calibrate(load_records(d))
            self.assertIn("Best weight: 0.", report)
            self.assertIn("LINEMAKER_MODEL_WEIGHT=", report)
            best = float(report.split("Best weight: ")[1][:4])
            self.assertGreater(best, 0.7)


class NewGatesTest(unittest.TestCase):
    def test_max_gap_and_min_ev(self):
        from linemaker.pro import rows_to_markets
        rows = [{"date": "2026-10-21", "sport": "NBA", "game_id": "g", "game": "A @ H",
                 "bet_type": "prop", "subject": "Way Off", "market": "points", "line": "10.5",
                 "mean": "30", "books": "FanDuel:-110/-110|DraftKings:-110/-110"},
                {"date": "2026-10-21", "sport": "NBA", "game_id": "g", "game": "A @ H",
                 "bet_type": "prop", "subject": "Thin Edge", "market": "points", "line": "20.5",
                 "mean": "21.5", "books": "FanDuel:-110/-110|DraftKings:-110/-110"}]
        res = run_pro(rows_to_markets(rows), "2026-10-21",
                      ProConfig(n_sims=4000, max_gap=0.15, min_ev=0.03, track=False, over_min_gap=None))
        reasons = {lbl.split(" Over")[0]: r for lbl, r in res.passes}
        self.assertIn("too large to trust", reasons["Way Off"])
        self.assertIn("EV", reasons["Thin Edge"])
        self.assertEqual(res.picks, [])

    def test_overs_need_a_bigger_gap(self):
        from unittest import mock
        from linemaker.pro import rows_to_markets
        row = {"date": "2026-10-21", "sport": "NBA", "game_id": "g", "game": "A @ H", "bet_type": "prop",
               "market": "points", "line": "20.5", "books": "FanDuel:-110/-110|DraftKings:-110/-110"}
        rows = [dict(row, subject="Small Over", mean="21.6"), dict(row, subject="Big Over", mean="26"),
                dict(row, subject="Small Under", mean="19.4")]
        res = run_pro(rows_to_markets(rows), "2026-10-21", ProConfig(n_sims=4000, track=False))
        picked = {ev.market.subject for ev in res.picks}
        self.assertEqual(picked, {"Big Over", "Small Under"})
        self.assertTrue(any("overs need more than 4%" in r for _l, r in res.passes))
        with mock.patch.dict("os.environ", {"LINEMAKER_OVER_MIN_GAP": "off"}):
            res = run_pro(rows_to_markets(rows), "2026-10-21", ProConfig(n_sims=4000, track=False))
        self.assertIn("Small Over", {ev.market.subject for ev in res.picks})


class VolumeEngineTest(unittest.TestCase):
    def _rows(self):
        base = {"date": "2026-10-21", "sport": "NBA", "bet_type": "prop", "lineup_confirmed": "true"}
        rows = []
        for i, (mean, line, odds) in enumerate(((24, 14.5, "-400/+300"), (9, 4.5, "-350/+260"),
                                                (12, 6.5, "-300/+230"), (30, 29.5, "-110/-110"))):
            rows.append(dict(base, game_id=f"g{i}", game=f"A{i} @ H{i}", subject=f"Player {i}",
                             market="points", line=str(line), mean=str(mean),
                             books=f"FanDuel:{odds}|DraftKings:{odds}"))
        return rows

    def test_volume_ranks_by_hit_chance_and_builds_parlays(self):
        from linemaker.parlay import build_parlays
        from linemaker.pro import rows_to_markets, volume_config
        cfg = volume_config(n_sims=4000, track=False, bet_books=("FanDuel", "DraftKings"))
        res = run_pro(rows_to_markets(self._rows()), "2026-10-21", cfg)
        probs = [ev.best.blended for ev in res.picks]
        self.assertEqual(probs, sorted(probs, reverse=True))
        self.assertTrue(all(p >= 0.75 for p in probs))
        self.assertFalse(any(ev.market.subject == "Player 3" for ev in res.picks))   # -110 coin flip
        parlays = build_parlays(res.picks, sizes=(2, 3))
        fd3 = next(p for p in parlays if p.book == "FanDuel" and len(p.legs) == 3)
        self.assertEqual(len({ev.market.game_id for ev, _o in fd3.legs}), 3)       # one per game
        expect = 1.0
        for ev, _o in fd3.legs:
            expect *= ev.best.blended
        self.assertAlmostEqual(fd3.prob, expect)
        self.assertGreater(fd3.american, 0)    # three heavy favorites still pay plus money

    def test_volume_layout(self):
        try:
            import discord  # noqa: F401
        except ImportError:
            self.skipTest("discord.py not installed")
        from linemaker.pro import rows_to_markets, volume_config
        from linemaker.scan_format import volume_messages
        cfg = volume_config(n_sims=4000, track=False, bet_books=("FanDuel", "DraftKings"))
        res = run_pro(rows_to_markets(self._rows()), "2026-10-21", cfg)
        msgs = volume_messages([("NBA", res, {})], "2026-10-21", cfg)
        titles = [e.title for group, _d in msgs for e in group]
        self.assertTrue(titles[0].startswith("🎰 NBA Volume Mode"))
        self.assertIn("✅ High-Hit-Rate Legs", titles)
        self.assertTrue(any(t.startswith("🟦 FanDuel · 🔒 2-leg Safest") for t in titles))   # one box per parlay
        self.assertTrue(any(t.startswith("🟩 DraftKings · ") for t in titles))
        self.assertIn("🧠 Volume Mode, Honestly", titles)


class MarketModeTest(unittest.TestCase):
    def _rows(self, fd_over):
        books = (f"FanDuel:{fd_over}/-110|DraftKings:-115/-105|Pinnacle:-120/+100|BetMGM:-118/-102|"
                 "Caesars:-115/-105")
        return [{"date": "2026-10-21", "sport": "NBA", "game_id": "g", "game": "A @ H", "bet_type": "prop",
                 "subject": "Soft Line", "market": "points", "line": "20.5", "mean": "15",
                 "books": books}]

    def test_stale_book_price_is_picked_without_the_model(self):
        from linemaker.pro import market_config, rows_to_markets
        res = run_pro(rows_to_markets(self._rows("+120")), "2026-10-21", market_config(track=False, n_sims=2000))
        self.assertEqual([e.best.label for e in res.picks], ["Soft Line Over 20.5 points"])   # model says under
        self.assertEqual(res.picks[0].model_weight, 0.0)
        self.assertAlmostEqual(res.picks[0].best.blended, res.picks[0].best.market_prob)

    def test_needs_enough_books_and_a_real_gap(self):
        from linemaker.pro import market_config, rows_to_markets
        res = run_pro(rows_to_markets(self._rows("-118")), "2026-10-21", market_config(track=False, n_sims=2000))
        self.assertEqual(res.picks, [])
        thin = self._rows("+120")
        thin[0]["books"] = "FanDuel:+120/-110|DraftKings:-115/-105"
        res = run_pro(rows_to_markets(thin), "2026-10-21", market_config(track=False, n_sims=2000))
        self.assertTrue(any("books with both sides" in r for _l, r in res.passes))

    def test_quick_words(self):
        from linemaker.chat import quick_mode, quick_plays_request
        self.assertEqual(quick_mode("best +ev props nba"), "market")
        self.assertEqual(quick_mode("sharp plays tonight"), "market")
        self.assertEqual(quick_mode("best nba props"), "smart")
        self.assertEqual(quick_plays_request("+ev nhl"), ["NHL"])
