import unittest
from types import SimpleNamespace as NS

from linemaker.parlay import sgp_check


def leg(game, bet_type, subject, market, line, side="A"):
    m = NS(game_id=game, game=f"{game} game", bet_type=bet_type, subject=subject, market=market,
           line=line, key=f"{game}|{subject}|{market}|{line}")
    return NS(market=m, best=NS(label=f"{subject} {market} {line} {side}", side=side))


class SGPTest(unittest.TestCase):
    def test_different_games_is_a_plain_parlay(self):
        r = sgp_check([leg("g1", "prop", "A", "points", 20), leg("g2", "prop", "B", "points", 20)], "DraftKings")
        self.assertEqual((r["kind"], r["blocked"]), ("", []))

    def test_one_game_is_an_sgp(self):
        legs = [leg("g1", "prop", "A", "points", 20), leg("g1", "prop", "A", "rebounds", 8),
                leg("g1", "prop", "B", "points", 15), leg("g1", "total", "g1 game", "points", 220)]
        r = sgp_check(legs, "DraftKings")
        self.assertEqual(r["kind"], "SGP")
        self.assertEqual(r["groups"], [("g1 game", 4)])
        self.assertEqual(r["blocked"], [])

    def test_sgp_plus_other_games(self):
        legs = [leg("g1", "prop", "A", "points", 20), leg("g1", "prop", "B", "points", 15),
                leg("g2", "moneyline", "T", "moneyline", 0)]
        self.assertEqual(sgp_check(legs, "DraftKings")["kind"], "SGPx")
        self.assertEqual(sgp_check(legs, "FanDuel")["kind"], "SGP+")

    def test_conflicts(self):
        two_lines = [leg("g1", "prop", "A", "points", 20), leg("g1", "prop", "A", "points", 25)]
        self.assertIn("two lines", sgp_check(two_lines, "DraftKings")["blocked"][0])
        ml_spread = [leg("g1", "moneyline", "T", "moneyline", 0), leg("g1", "spread", "T", "spread", -4.5)]
        self.assertTrue(sgp_check(ml_spread, "DraftKings")["blocked"])


if __name__ == "__main__":
    unittest.main()


class AutoSGPTest(unittest.TestCase):
    def _legs(self):
        from unittest import mock
        legs = []
        spec = [("g1", "A", "points", 20, .85), ("g1", "A", "points", 25, .84),   # clash: same stat
                ("g1", "A", "rebounds", 8, .83), ("g1", "B", "assists", 6, .80),
                ("g2", "C", "points", 18, .82), ("g2", "D", "rebounds", 9, .79),
                ("g3", "E", "points", 12, .81)]
        for g, who, stat, line, p in spec:
            ev = leg(g, "prop", who, stat, line)
            ev.best.blended = p
            legs.append(ev)
        return legs

    def test_builds_sgp_and_sgpx(self):
        from unittest import mock
        from linemaker import parlay
        with mock.patch.object(parlay, "leg_price", lambda ev, book: -300):
            out = parlay.build_sgps(self._legs(), books=("DraftKings",))
        kinds = {p.kind: p for p in out}
        sgp, sgpx = kinds["SGP"], kinds["SGPx"]
        self.assertEqual({ev.market.game_id for ev, _o in sgp.legs}, {"g1"})
        self.assertEqual(len(sgp.legs), 3)
        self.assertEqual(sgp_check([ev for ev, _o in sgp.legs], "DraftKings")["blocked"], [])
        self.assertEqual(len(sgpx.legs), 4)
        self.assertEqual({ev.market.game_id for ev, _o in sgpx.legs}, {"g1", "g2"})
        self.assertEqual(sgp_check([ev for ev, _o in sgpx.legs], "DraftKings")["blocked"], [])
        with mock.patch.object(parlay, "leg_price", lambda ev, book: -300):
            self.assertEqual({p.kind for p in parlay.build_sgps(self._legs(), books=("FanDuel",))},
                             {"SGP", "SGP+"})
