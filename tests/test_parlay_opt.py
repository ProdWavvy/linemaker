import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from linemaker import parlay, parlay_math, parlay_track


def leg(game, subject, market, side="A", p=0.6, bet_type="prop", key=None, odds=-150, opponent=""):
    m = NS(game_id=game, game=f"{game} game", sport="NBA", date="2026-10-24", bet_type=bet_type,
           subject=subject, opponent=opponent, market=market, line=10.5, key=key or f"{game}|{subject}|{market}",
           books=[("FanDuel", [odds, 120] if side == "A" else [120, odds])], side_keys=("A", "B"), links={})
    return NS(market=m, best=NS(side=side, label=f"{subject} {market} {side}", blended=p,
                                best_odds=odds, best_book="FanDuel"))


class CorrelationTest(unittest.TestCase):
    def test_pairs(self):
        self.assertEqual(parlay_math.pair_rho(leg("g1", "A", "points"), leg("g2", "B", "points")), 0.0)
        self.assertAlmostEqual(parlay_math.pair_rho(leg("g1", "A", "points"), leg("g1", "A", "pra")), 0.7)
        self.assertAlmostEqual(parlay_math.pair_rho(leg("g1", "A", "points"), leg("g1", "A", "rebounds", "B")), -0.3)
        total = leg("g1", "g1", "game total", bet_type="total")
        self.assertAlmostEqual(parlay_math.pair_rho(total, leg("g1", "A", "points")), 0.25)

    def test_teammates_qb_and_receiver(self):
        teams = {"qb one": "Lions", "wr one": "Lions", "wr two": "Bears"}
        with mock.patch.object(parlay_math, "team_of", side_effect=lambda s, d, p: teams.get(p.lower())):
            qb, wr = leg("g", "QB One", "passing yards"), leg("g", "WR One", "receiving yards")
            other = leg("g", "WR Two", "receiving yards")
            self.assertAlmostEqual(parlay_math.pair_rho(qb, wr), 0.4)
            self.assertAlmostEqual(parlay_math.pair_rho(qb, other), 0.08)

    def test_joint_prob(self):
        a, b = leg("g1", "A", "points", p=0.6), leg("g1", "A", "pra", p=0.6)
        self.assertGreater(parlay_math.joint_prob([a, b]), 0.36 + 0.05)     # overlapping stats hit together
        c = leg("g1", "A", "rebounds", "B", p=0.6)
        self.assertLess(parlay_math.joint_prob([a, c]), 0.36)               # over + under: pull apart
        d = leg("g2", "B", "points", p=0.6)
        self.assertAlmostEqual(parlay_math.joint_prob([a, d]), 0.36)        # different games: product

    def test_boost(self):
        self.assertAlmostEqual(parlay_math.boosted(3.0, 0.25), 3.5)


class ValueParlayTest(unittest.TestCase):
    def test_value_parlays_only_when_plus_ev(self):
        legs = [leg(f"g{i}", f"P{i}", "points", p=0.62, odds=-120) for i in range(4)]   # value 1.14 each
        legs += [leg(f"h{i}", f"Q{i}", "points", p=0.876, odds=-700) for i in range(4)]  # likelier, fair
        with mock.patch.object(parlay, "leg_price", side_effect=lambda ev, b: ev.best.best_odds):
            out = parlay.build_parlays(legs, sizes=(2, 3), books=("FanDuel",))
        value = [p for p in out if p.style == "value"]
        self.assertEqual({len(p.legs) for p in value}, {2, 3})
        self.assertTrue(all(ev.best.blended == 0.62 for p in value for ev, _o in p.legs))
        self.assertTrue(all(p.prob * p.decimal > 1 for p in value))
        safest = next(p for p in out if p.style == "safest" and len(p.legs) == 2)
        self.assertTrue(all(ev.best.blended == 0.876 for ev, _o in safest.legs))

    def test_safest_skips_minus_ev_favourites(self):
        legs = [leg(f"g{i}", f"P{i}", "points", p=0.62, odds=-120) for i in range(3)]
        legs += [leg(f"h{i}", f"Q{i}", "points", p=0.85, odds=-700) for i in range(3)]   # value 0.97
        with mock.patch.object(parlay, "leg_price", side_effect=lambda ev, b: ev.best.best_odds):
            out = parlay.build_parlays(legs, sizes=(2,), books=("FanDuel",))
        safest = next(p for p in out if p.style == "safest")
        self.assertTrue(all(ev.best.blended == 0.62 for ev, _o in safest.legs))


class ParlayTrackTest(unittest.TestCase):
    def test_log_and_grade(self):
        d = tempfile.mkdtemp()
        a, b = leg("g1", "A", "points", key="k1"), leg("g2", "B", "points", key="k2")
        p = parlay.Parlay(book="FanDuel", legs=[(a, -150), (b, -150)], prob=0.36, decimal=2.78)
        with mock.patch.dict(os.environ, {"LINEMAKER_TRACKING_DIR": d, "LINEMAKER_TRACKING": "on"}):
            parlay_track.log_parlays([p, p], "2026-10-24")
            self.assertEqual(len(parlay_track._load("2026-10-24")), 1)          # no duplicates
            with open(os.path.join(d, "2026-10-24.json"), "w") as f:
                json.dump({"k1": {"result": {"winner": "A"}}, "k2": {"result": {"winner": "A"}}}, f)
            self.assertEqual(parlay_track.grade_parlays("2026-10-24"), 1)
            done = list(parlay_track._load("2026-10-24").values())
            self.assertEqual(done[0]["result"], "won")
            self.assertIn("1/1 hit", "\n".join(parlay_track.summary_lines(done)))


if __name__ == "__main__":
    unittest.main()


class TargetTest(unittest.TestCase):
    def test_highest_hit_chance_for_the_payout(self):
        legs = [leg(f"f{i}", f"Fav{i}", "points", p=0.86, odds=-600) for i in range(6)]    # fair-ish, pays 1.17x
        legs += [leg("v1", "Value1", "points", p=0.62, odds=-120), leg("v2", "Value2", "points", p=0.60, odds=-115)]
        with mock.patch.object(parlay, "leg_price", side_effect=lambda ev, b: ev.best.best_odds):
            p = parlay.best_for_target(legs, "FanDuel", 3.0)
        self.assertGreaterEqual(p.decimal, 3.0)
        self.assertEqual(len({ev.market.game_id for ev, _o in p.legs}), len(p.legs))     # one per game
        favs_only = 0.86 ** 7                                                              # 7 favourites for 3x
        self.assertGreater(p.prob, favs_only)
        self.assertTrue(any(ev.best.label.startswith("Value") for ev, _o in p.legs))

    def test_unreachable_target(self):
        legs = [leg("g1", "A", "points", p=0.86, odds=-600)]
        with mock.patch.object(parlay, "leg_price", side_effect=lambda ev, b: ev.best.best_odds):
            self.assertIsNone(parlay.best_for_target(legs, "FanDuel", 6.0))


class TargetCardTest(unittest.TestCase):
    def test_card(self):
        import asyncio
        from linemaker import bot
        legs = [leg(f"g{i}", f"P{i}", "points", p=0.62, odds=-120) for i in range(8)]
        with mock.patch.object(parlay, "leg_price", side_effect=lambda ev, b: ev.best.best_odds):
            async def build():
                return bot.targets_card([{"rank": i, "ev": ev} for i, ev in enumerate(legs, 1)], ("FanDuel",))
            embeds, view = asyncio.run(build())
        self.assertEqual(embeds[0].title, "🎯 Safest Parlay for Each Payout")
        boxes = embeds[1:]
        self.assertTrue(all(b.title.startswith("Payout +") for b in boxes))
        field = boxes[0].fields[0]
        self.assertRegex(field.name, r"^🟦 FanDuel · \+\d+ · \d+% hit")       # numbers in the header
        self.assertIn("• P", field.value)                                         # one leg per line
        self.assertIn("💵", field.value)
        self.assertIn("Roll it over", field.value)
        self.assertTrue(any((getattr(c, "label", "") or "").startswith("Bet +") for c in view.children))

    def test_repeat_is_merged(self):
        import asyncio
        from linemaker import bot
        a = leg("g1", "A", "points", p=0.6, odds=+130)
        b = leg("g2", "B", "points", p=0.6, odds=+130)
        p = parlay.price("FanDuel", [(a, 130), (b, 130)])    # pays 5.29x: covers +100 through +300
        found = {(t, "FanDuel"): (p if t <= 5.29 else None) for t in parlay.TARGETS}

        async def build():
            return bot.targets_card([], ("FanDuel",), found)
        embeds, _v = asyncio.run(build())
        self.assertEqual(embeds[1].title, "Payout +100 · +200 · +300")
        self.assertEqual(len(embeds[1].fields), 1)
        self.assertEqual(embeds[2].title, "Payout +500")
        self.assertIn("No combination", embeds[2].description)


class SameGameTargetTest(unittest.TestCase):
    def test_one_game_builds_an_sgp(self):
        legs = [leg("g1", f"P{i}", "points", p=0.62, odds=-120) for i in range(5)]
        with mock.patch.object(parlay, "leg_price", side_effect=lambda ev, b: ev.best.best_odds):
            p = parlay.best_for_target(legs, "FanDuel", 3.0)
        self.assertGreaterEqual(p.decimal, 3.0)
        self.assertEqual(p.kind, "SGP")
        self.assertEqual(len({ev.market.subject for ev, _o in p.legs}), len(p.legs))   # one per player


class LadderSpreadTest(unittest.TestCase):
    def _legs(self):
        legs = [leg("chalk", "Chalk", "points", p=0.80, odds=-250)]                     # the standout leg
        legs += [leg(f"g{i}", f"P{i}", "points", p=0.60 - i * 0.005, odds=-115) for i in range(10)]
        return legs

    def test_payouts_dont_all_ride_one_leg(self):
        from linemaker import bot
        legs = self._legs()
        with mock.patch.object(parlay, "leg_price", side_effect=lambda ev, b: ev.best.best_odds):
            found = bot.target_parlays([{"rank": i, "ev": ev} for i, ev in enumerate(legs, 1)], ("FanDuel",))
        tickets = [p for p in found.values() if p]
        self.assertEqual(len(tickets), len(parlay.TARGETS))
        uses = {}
        for p in tickets:
            for ev, _o in p.legs:
                uses[ev.market.subject] = uses.get(ev.market.subject, 0) + 1
        self.assertLessEqual(max(uses.values()), 2)                    # no leg in more than 2 payouts
        self.assertLessEqual(uses.get("Chalk", 0), 2)
        seen = set()
        for t in parlay.TARGETS:                                       # at most 1 shared leg each
            p = found[(t, "FanDuel")]
            subs = {ev.market.subject for ev, _o in p.legs}
            self.assertLessEqual(len(subs & seen), 1)
            seen |= subs

    def test_card_flags_shared_legs(self):
        import asyncio
        from linemaker import bot
        legs = [leg(f"g{i}", f"P{i}", "points", p=0.62, odds=-120) for i in range(6)]    # small pool
        with mock.patch.object(parlay, "leg_price", side_effect=lambda ev, b: ev.best.best_odds):
            async def build():
                return bot.targets_card([{"rank": i, "ev": ev} for i, ev in enumerate(legs, 1)], ("FanDuel",))
            embeds, _v = asyncio.run(build())
        self.assertIn("Spread out", embeds[0].description)
        text = "\n".join(f.value for e in embeds[1:] for f in e.fields)
        self.assertIn("🔗 Shares", text)
        self.assertNotIn("No combination", "\n".join(e.description or "" for e in embeds[1:]))

    def test_tiny_pool_falls_back_and_says_so(self):
        from linemaker import bot
        legs = [leg(f"g{i}", f"P{i}", "points", p=0.62, odds=-120) for i in range(3)]
        with mock.patch.object(parlay, "leg_price", side_effect=lambda ev, b: ev.best.best_odds):
            found = bot.target_parlays([{"rank": i, "ev": ev} for i, ev in enumerate(legs, 1)], ("FanDuel",))
        flagged = [p for p in found.values() if p and getattr(p, "reuses", False)]
        self.assertTrue(flagged)


class ParlayCalibrationTest(unittest.TestCase):
    def _graded(self, n, days, truth_scale, seed=3):
        import random
        rng = random.Random(seed)
        out = []
        for i in range(n):
            p = rng.uniform(0.25, 0.7)                       # what the parlay said
            won = rng.random() < p * truth_scale             # what happened
            out.append({"prob": p, "raw_prob": p, "result": "won" if won else "lost",
                        "logged": f"2026-10-{1 + i % days:02d}T12:00:00",
                        "legs": [{"kind": "props", "live": False}] * 3})
        return out

    def test_fits_overconfident_parlays(self):
        from linemaker import autotune
        out, notes = autotune.tune_parlay_calibration(self._graded(900, 20, 0.7))
        self.assertIn("pre props", out)
        a, b, n = out["pre props"]
        self.assertLess(autotune.calibrate_p(0.4, a, b), 0.36)          # said 40% -> lower
        self.assertTrue(notes)

    def test_waits_for_enough_and_ignores_noise(self):
        from linemaker import autotune
        self.assertEqual(autotune.tune_parlay_calibration(self._graded(100, 20, 0.7))[0], {})   # too few
        self.assertEqual(autotune.tune_parlay_calibration(self._graded(900, 2, 0.7))[0], {})    # 2 nights only
        self.assertEqual(autotune.tune_parlay_calibration(self._graded(900, 20, 1.0))[0], {})   # honest already

    def test_price_uses_it(self):
        from linemaker import autotune
        a, b = leg("g1", "A", "points", p=0.7, odds=-200), leg("g2", "B", "points", p=0.7, odds=-200)
        fit = {"parlay_calibration": {"pre props": [-0.5, 1.0, 500]}}
        with mock.patch.object(autotune, "load_weights", return_value=fit):
            p = parlay.price("FanDuel", [(a, -200), (b, -200)])
        self.assertAlmostEqual(p.raw_prob, 0.49, places=2)
        self.assertLess(p.prob, p.raw_prob)


class ExposureTest(unittest.TestCase):
    def test_leg_in_open_bets_left_out_of_new_parlays(self):
        import tempfile
        from linemaker import bets, bot
        from linemaker.pro import rows_to_markets, run_pro, volume_config
        from linemaker.scan_format import volume_messages
        rows = [{"date": "2026-10-25", "sport": "NFL", "game_id": f"g{i}", "game": f"A{i} @ H{i}",
                 "bet_type": "prop", "subject": "J.K. Dobbins" if i == 0 else f"P{i}", "market": "rushing yards",
                 "line": "24.5", "mean": str(60 - i), "stdev": "15",
                 "books": "FanDuel:-600/+400|DraftKings:-600/+400"} for i in range(6)]
        cfg = volume_config()
        cfg.min_prob, cfg.min_odds, cfg.max_odds, cfg.min_gap, cfg.track = 0.5, -100, -10000, -1.0, False
        res = run_pro(rows_to_markets(rows), "2026-10-25", cfg)
        dobbins = next(ev for ev in res.picks if ev.market.subject == "J.K. Dobbins")
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LINEMAKER_TRACKING_DIR": d}):
            for _ in range(2):
                bets.add_bet([bets.leg_from_eval(dobbins)], -600, 10, "FanDuel")
            self.assertEqual(bets.open_exposure()[("g0", "J.K. Dobbins")]["n"], 2)
            msgs = volume_messages([("NFL", res, {"profiles": {}, "context": {}, "history": {}})], "2026-10-25", cfg)
            parlays = [p for _e, dd in msgs if dd and "parlays" in dd for p in dd["parlays"]]
            self.assertTrue(parlays)
            self.assertFalse(any(ev.market.subject == "J.K. Dobbins" for p in parlays for ev, _o in p.legs))
            self.assertIn("Left out", msgs[0][0][0].description)
            found = bot.target_parlays([{"rank": i, "ev": ev} for i, ev in enumerate(res.picks, 1)], ("FanDuel",))
            self.assertFalse(any(ev.market.subject == "J.K. Dobbins" for p in found.values() if p
                                 and not getattr(p, "reuses", False) for ev, _o in p.legs))


class ParlaySystemFixTest(unittest.TestCase):
    def test_same_game_priced_like_an_sgp(self):
        a = leg("g1", "A", "points", p=0.6, odds=-120)
        b = leg("g1", "A", "pra", p=0.6, odds=-120)          # same player, overlapping stats: rho 0.7
        with mock.patch.dict(os.environ, {"LINEMAKER_SGP_MARGIN": "0.05"}):
            p = parlay.price("FanDuel", [(a, -120), (b, -120)])
        self.assertTrue(p.sgp_estimated)
        self.assertLess(p.decimal, p.listed_decimal)
        self.assertGreater(p.prob, p.indep_prob)                       # they do move together...
        self.assertAlmostEqual(p.prob * p.decimal, p.indep_prob * p.listed_decimal * 0.95, places=3)  # ...but it isn't free
        other = parlay.price("FanDuel", [(a, -120), (leg("g2", "B", "points", p=0.6, odds=-120), -120)])
        self.assertFalse(other.sgp_estimated)
        self.assertEqual(other.decimal, other.listed_decimal)

    def test_best_next_leg_balances_hit_chance_and_value(self):
        from linemaker import bot
        picks = [{"rank": 1, "ev": leg("g1", "Anchor", "points", p=0.65, odds=-150)},
                 {"rank": 2, "ev": leg("g2", "Longshot", "points", p=0.30, odds=+260)},     # value 1.08, 30% hit
                 {"rank": 3, "ev": leg("g3", "Solid", "points", p=0.66, odds=-150)},        # value 1.10, 66% hit
                 {"rank": 4, "ev": leg("g1", "Anchor", "rebounds", p=0.7, odds=-150)}]      # same player
        with mock.patch.object(parlay, "leg_price", side_effect=lambda ev, b: ev.best.best_odds), \
                mock.patch("linemaker.bets.open_exposure", return_value={}):
            self.assertEqual(bot.best_next_leg(picks, [0], ("FanDuel",)), 2)        # Solid, not the long shot
            self.assertEqual(bot.best_next_leg(picks, [0, 2, 1], ("FanDuel",)), None)   # only Anchor left: same player

    def test_best_next_leg_skips_what_you_already_hold(self):
        from linemaker import bot
        picks = [{"rank": 1, "ev": leg("g1", "Anchor", "points", p=0.65, odds=-150)},
                 {"rank": 2, "ev": leg("g2", "Held", "points", p=0.7, odds=-140)},
                 {"rank": 3, "ev": leg("g3", "Other", "points", p=0.62, odds=-150)}]
        held = {("g2", "Held"): {"n": 2, "label": "Held points A"}}
        with mock.patch.object(parlay, "leg_price", side_effect=lambda ev, b: ev.best.best_odds), \
                mock.patch("linemaker.bets.open_exposure", return_value=held):
            self.assertEqual(bot.best_next_leg(picks, [0], ("FanDuel",)), 2)

    def test_edge_parlays_from_model_picks(self):
        legs = []
        for i, (p, o, gap) in enumerate(((0.62, -120, 0.08), (0.60, -115, 0.07), (0.58, -110, 0.06), (0.50, -150, -0.1))):
            ev = leg(f"g{i}", f"E{i}", "points", p=p, odds=o)
            ev.best.gap = gap
            legs.append(ev)
        with mock.patch.object(parlay, "leg_price", side_effect=lambda ev, b: ev.best.best_odds):
            out = parlay.build_edge_parlays(legs, sizes=(2, 3), books=("FanDuel",))
        self.assertTrue(out)
        self.assertTrue(all(p.style == "edge" and p.prob * p.decimal > 1 for p in out))
        self.assertFalse(any(ev.market.subject == "E3" for p in out for ev, _o in p.legs))   # -EV leg left out
        self.assertEqual([ev.market.subject for ev, _o in out[0].legs], ["E0", "E1"])       # biggest edge first

    def test_sgp_graded_at_its_price(self):
        import tempfile
        a, b = leg("g1", "A", "points", key="k1", odds=-120), leg("g1", "A", "pra", key="k2", odds=-120)
        p = parlay.price("FanDuel", [(a, -120), (b, -120)])
        d = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"LINEMAKER_TRACKING_DIR": d, "LINEMAKER_TRACKING": "on"}):
            parlay_track.log_parlays([p], "2026-10-24")
            with open(os.path.join(d, "2026-10-24.json"), "w") as f:
                json.dump({"k1": {"result": {"winner": "A"}}, "k2": {"result": {"winner": "A"}}}, f)
            parlay_track.grade_parlays("2026-10-24")
            done = list(parlay_track._load("2026-10-24").values())[0]
        self.assertAlmostEqual(done["profit"], round(p.decimal - 1, 4), places=3)
        self.assertLess(done["profit"], p.listed_decimal - 1)
