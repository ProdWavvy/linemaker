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
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["subject"], "Movsar Evloev")
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


if __name__ == "__main__":
    unittest.main()
