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
            embed, view = asyncio.run(build())
        self.assertEqual(len(embed.fields), 4)
        self.assertIn("in a row", embed.fields[1].value)
        self.assertTrue(any((getattr(c, "label", "") or "").startswith("Log +200") for c in view.children))
