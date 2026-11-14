import os
import tempfile
import unittest
from unittest import mock

from linemaker import ufc_props_track as t


def comp(a, b, winner, method_text, clock="5:00", period=3, rounds=3, completed=True, uid="c1"):
    return {"uid": uid, "status": {"type": {"completed": completed}, "displayClock": clock, "period": period},
            "format": {"regulation": {"periods": rounds}},
            "details": [{"type": {"text": method_text}}, {"type": {"text": "Fight Over"}}] if method_text else [],
            "competitors": [{"athlete": {"displayName": a}, "winner": winner == a},
                            {"athlete": {"displayName": b}, "winner": winner == b}]}


class OutcomeTest(unittest.TestCase):
    def test_methods_rounds_and_voids(self):
        dec = t.fight_outcome(comp("A", "B", "A", "Unofficial Winner Decision"))
        self.assertEqual((dec["method"], dec["winner"], dec["round"]), ("DEC", "A", 3))
        sub = t.fight_outcome(comp("A", "B", "B", "Unofficial Winner Submission", clock="3:23", period=1))
        self.assertEqual((sub["method"], sub["round"]), ("SUB", 1))
        self.assertAlmostEqual(sub["minutes"], 3 + 23 / 60, places=3)
        ko = t.fight_outcome(comp("A", "B", "A", "Unofficial Winner KO/TKO", clock="1:10", period=2))
        self.assertEqual(ko["method"], "KO")
        self.assertTrue(t.fight_outcome(comp("A", "B", None, "Draw"))["void"])
        self.assertIsNone(t.fight_outcome(comp("A", "B", "A", "", completed=False)))
        quiet = t.fight_outcome(comp("A", "B", "A", ""))   # no method text, went the full time
        self.assertEqual(quiet["method"], "DEC")

    def test_yes_sides(self):
        sub = t.fight_outcome(comp("Macy Chiasson", "Bia Mesquita", "Bia Mesquita", "Unofficial Winner Submission",
                                   clock="3:23", period=1))
        self.assertFalse(t.yes_happened({"kind": "distance"}, sub))
        self.assertTrue(t.yes_happened({"kind": "rounds", "before": 2}, sub))
        self.assertTrue(t.yes_happened({"kind": "mov", "fighter": "Beatriz Mesquita", "method": "SUB"}, sub))
        self.assertFalse(t.yes_happened({"kind": "mov", "fighter": "Macy Chiasson", "method": "SUB"}, sub))
        self.assertTrue(t.yes_happened({"kind": "stat", "line": 20.5}, sub, 25))
        self.assertIsNone(t.yes_happened({"kind": "stat", "line": 20}, sub, 20))   # push


class LogAndGradeTest(unittest.TestCase):
    def test_log_grade_and_summary(self):
        card = [("Macy Chiasson", "1", "Bia Mesquita", "2",
                 comp("Macy Chiasson", "Bia Mesquita", "Bia Mesquita", "Unofficial Winner Submission",
                      clock="3:23", period=1))]
        entries = [{"fight": "Macy Chiasson vs Beatriz Mesquita", "label": "Doesn't go the distance",
                    "spec": {"kind": "distance"}, "yes": False, "chance": 0.7, "price": 0.6, "book": "Kalshi",
                    "shown": True},
                   {"fight": "Macy Chiasson vs Beatriz Mesquita", "label": "Macy Chiasson by KO/TKO",
                    "spec": {"kind": "mov", "fighter": "Macy Chiasson", "method": "KO"}, "yes": True,
                    "chance": 0.2, "price": 0.15, "book": "Kalshi", "shown": False},
                   {"fight": "Macy Chiasson vs Beatriz Mesquita", "label": "Bia Mesquita over 40.5 sig strikes",
                    "spec": {"kind": "stat", "stat": "significant strikes", "line": 40.5, "fighter": "Bia Mesquita"},
                    "yes": True, "chance": 0.6, "odds": None, "book": "PrizePicks", "shown": True}]
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LINEMAKER_TRACKING_DIR": d, "LINEMAKER_TRACKING": "on"}):
            self.assertEqual(t.log_props("2026-11-14", entries), 3)
            self.assertEqual(t.log_props("2026-11-14", entries), 0)          # first sighting wins
            with mock.patch("linemaker.ufc_model.card", return_value=card), \
                    mock.patch.object(t, "_stat_value", return_value=12):
                self.assertEqual(t.grade("2026-11-14"), 3)
            data = t.load("2026-11-14")
            res = {v["label"]: (v["result"], v.get("profit")) for v in data.values()}
            self.assertEqual(res["Doesn't go the distance"], ("won", round(1 / 0.6 - 1, 4)))
            self.assertEqual(res["Macy Chiasson by KO/TKO"], ("lost", -1.0))
            self.assertEqual(res["Bia Mesquita over 40.5 sig strikes"], ("lost", None))   # pick'em: no price

    def test_stats_not_posted_yet_wait(self):
        card = [("A One", "1", "B Two", "2", comp("A One", "B Two", "A One", "Unofficial Winner Decision"))]
        e = [{"fight": "A One vs B Two", "label": "A over 50.5", "yes": True, "chance": 0.5, "book": "PrizePicks",
              "spec": {"kind": "stat", "stat": "significant strikes", "line": 50.5, "fighter": "A One"}, "shown": True}]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"LINEMAKER_TRACKING_DIR": d, "LINEMAKER_TRACKING": "on"}), \
                mock.patch("linemaker.ufc_model.card", return_value=card), \
                mock.patch.object(t, "_stat_value", return_value=None):
            t.log_props("2026-11-14", e)
            with mock.patch.object(t, "_stale", return_value=False):
                self.assertEqual(t.grade("2026-11-14"), 0)                 # not yet: try again later
            with mock.patch.object(t, "_stale", return_value=True):
                self.assertEqual(t.grade("2026-11-14"), 1)                 # days later: void
            self.assertEqual(next(iter(t.load("2026-11-14").values()))["result"], "void")
            lines = t.summary_lines("2026-11-14")
            self.assertTrue(lines[0].startswith("Plays shown: **1-1**"))
            self.assertIn("Every prop priced: **1-2**", lines[1])


class CardTest(unittest.TestCase):
    def test_card_shape_and_logging(self):
        from linemaker import ufc_props
        priced = [{"fight": "A vs B", "props": [
            {"label": "Doesn't go the distance", "chance": 0.44, "market": 0.36, "model": 0.6, "kalshi_price": 0.37,
             "yes": False, "spec": {"kind": "distance"}}]}]
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"LINEMAKER_TRACKING_DIR": d, "LINEMAKER_TRACKING": "on", "PROPLINE_API_KEY": ""}), \
                mock.patch("linemaker.ufc_model.next_card_date", return_value="2026-11-14"), \
                mock.patch.object(ufc_props, "props_lines", return_value=([], priced)):
            embeds, text = ufc_props.props_card("2026-11-14")
            logged = t.load("2026-11-14")
        titles = [e.title for e in embeds]
        self.assertTrue(titles[0].startswith("🥊 UFC Props"))
        self.assertIn("🎯 Fight Props", titles)
        self.assertIn("📖 How These Are Priced", titles)
        self.assertIn("Doesn't go the distance · **44%**", embeds[1].description)
        self.assertIn("bet at **+132** or better", embeds[1].description)
        self.assertEqual(len(logged), 1)
        self.assertTrue(next(iter(logged.values()))["shown"])
