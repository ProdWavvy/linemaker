import unittest
from unittest import mock

from linemaker import ufc_model


def fighter(**kw):
    base = {"wins": 10, "losses": 3, "finishes": 6, "slpm": 4.0, "sacc": 50.0, "td": 1.5,
            "tdacc": 40.0, "sub": 0.5, "reach": 72.0, "age": 29, "form": [1, 1, -1, 1, 1]}
    base.update(kw)
    return base


class UFCModelTest(unittest.TestCase):
    def test_even_fight_is_a_coin_flip(self):
        p, _ = ufc_model.win_prob(fighter(), fighter())
        self.assertAlmostEqual(p, 0.5, places=6)

    def test_better_fighter_favoured_and_capped(self):
        good = fighter(wins=20, losses=1, form=[1] * 5, slpm=6.0, td=4.0)
        bad = fighter(wins=5, losses=8, form=[-1] * 5, slpm=2.0, td=0.2, age=38)
        p, steps = ufc_model.win_prob(good, bad)
        self.assertGreater(p, 0.75)
        self.assertLessEqual(p, 1 / (1 + 2.718281828 ** -2.2) + 1e-9)
        self.assertTrue(any(k == "form" for k, _v in steps))
        q, _ = ufc_model.win_prob(bad, good)
        self.assertAlmostEqual(p + q, 1.0, places=6)

    def test_missing_stats_sit_out(self):
        p, _ = ufc_model.win_prob(fighter(slpm=None), fighter(slpm=9.0))
        self.assertAlmostEqual(p, 0.5, places=6)

    def _card(self):
        comp = lambda a, b, wa=None: {"status": {"type": {"completed": wa is not None}},
                                      "competitors": [{"athlete": {"displayName": a}, "winner": wa},
                                                      {"athlete": {"displayName": b}, "winner": None if wa is None else not wa}]}
        return [("Alexander Volkanovski", "1", "Movsar Evloev", "2", comp("Alexander Volkanovski", "Movsar Evloev", False)),
                ("Merab Dvalishvili", "3", "Some One", "4", comp("Merab Dvalishvili", "Some One"))]

    def test_projection_rows_match_feed_names(self):
        info = {"1": fighter(), "2": fighter(wins=20, losses=0, form=[1] * 5), "3": fighter(), "4": fighter()}
        with mock.patch.object(ufc_model, "card", return_value=self._card()), \
                mock.patch.object(ufc_model, "fighter", side_effect=lambda i: info[i]):
            rows = ufc_model.ufc_projections("2026-10-24", [("Alexander Volkanovski", "Movsar Evloev")])
        self.assertEqual([r["market"] for r in rows], ["moneyline", "total rounds"])
        self.assertEqual(rows[0]["subject"], "Movsar Evloev")
        self.assertEqual(rows[1]["dist"], "ufcrounds")
        self.assertEqual(rows[1]["stdev"], "3")
        self.assertTrue(0.2 < float(rows[1]["mean"]) < 0.8)
        self.assertGreater(float(rows[0]["mean"]), 0)          # the stronger record is the home side here

    def test_results_from_espn(self):
        recs = {"k": {"sport": "UFC", "game_id": "g", "subject": "Movsar Evloev", "market": "moneyline",
                      "inputs": {"line": 0.0}, "result": None},
                "k2": {"sport": "UFC", "game_id": "g2", "subject": "Merab Dvalishvili", "market": "moneyline",
                       "inputs": {"line": 0.0}, "result": None}}
        with mock.patch.object(ufc_model, "card", return_value=self._card()):
            rows = ufc_model.ufc_results(recs, "2026-10-24")
        self.assertEqual(rows, [{"game_id": "g", "subject": "Movsar Evloev", "market": "moneyline",
                                 "line": "0.0", "actual": "1"}])   # Merab's fight isn't final


class RoundsTest(unittest.TestCase):
    def test_rounds_distribution(self):
        from linemaker.sim import simulate
        r = simulate(0.5, 2.5, "ufcrounds", 3, 40000, "x")
        # half go the distance (over 2.5) plus early finishes after 2:30 of round 3
        self.assertTrue(0.52 < r.p_over < 0.58)
        r1 = simulate(0.5, 1.5, "ufcrounds", 3, 40000, "x")
        self.assertGreater(r1.p_over, r.p_over)
        self.assertAlmostEqual(simulate(0.0, 2.5, "ufcrounds", 3, 1000, "x").p_over, 1.0)

    def test_fight_length(self):
        self.assertAlmostEqual(ufc_model.fight_length({"period": 2, "displayClock": "1:44"}), 1 + 104 / 300)
        self.assertEqual(ufc_model.fight_length({"period": 3, "displayClock": "5:00"}), 3.0)

    def test_finish_chance(self):
        killer = fighter(wins=20, finishes=18)
        chin = fighter(losses=10, finished=9)
        grinder = fighter(wins=20, finishes=2, losses=2, finished=0)
        self.assertGreater(ufc_model.finish_chance(killer, chin, 0.7),
                           ufc_model.finish_chance(grinder, grinder, 0.5))


if __name__ == "__main__":
    unittest.main()
