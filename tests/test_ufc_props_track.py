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
            lines = t.summary_lines("2026-11-14")
            self.assertTrue(lines[0].startswith("Plays shown: **1-1**"))
            self.assertIn("Every prop priced: **1-2**", lines[1])

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
            embeds, text, buttons = ufc_props.props_card("2026-11-14")
            logged = t.load("2026-11-14")
        titles = [e.title for e in embeds]
        self.assertTrue(titles[0].startswith("🥊 UFC Props"))
        self.assertIn("🎯 Fight Props", titles)
        self.assertIn("📖 How These Are Priced", titles)
        self.assertIn("Doesn't go the distance · **44%**", embeds[1].description)
        self.assertIn("bet at **+132**+", embeds[1].description)
        self.assertEqual(buttons, [])                                # no PropLine key: no book prices
        self.assertEqual(len(logged), 1)
        self.assertTrue(next(iter(logged.values()))["shown"])


class OffersTest(unittest.TestCase):
    def test_fight_offers_and_buttons(self):
        from linemaker import propline, ufc_props
        odds = {"bookmakers": [
            {"key": "draftkings", "title": "DraftKings", "link": "https://sportsbook.draftkings.com/event/34", "markets": [
                {"key": "fight_distance", "outcomes": [
                    {"name": "Yes", "price": 215, "book_outcome_id": "0QA#1"},
                    {"name": "No", "price": -295, "book_outcome_id": "0QA#2"}]},
                {"key": "round_betting", "outcomes": [
                    {"name": "Ciryl Gane by Decision", "price": 350, "book_outcome_id": "0QA#3"}]}]},
            {"key": "fanduel", "title": "FanDuel", "markets": [
                {"key": "fight_distance", "outcomes": [
                    {"name": "No", "price": -310, "book_outcome_id": "734.19:76"}]},
                {"key": "round_betting", "outcomes": [
                    {"name": "Ciryl Gane to Win by Decision", "price": 400, "book_outcome_id": "734.19:97"}]}]}]}
        with mock.patch.dict(os.environ, {"PROPLINE_API_KEY": "k"}), \
                mock.patch.object(propline, "events", return_value=[{"id": "1", "home_team": "Josh Hokit",
                                                                     "away_team": "Ciryl Gane"}]), \
                mock.patch.object(propline, "event_odds", return_value=odds), \
                mock.patch.object(propline, "_call", return_value={"bookmakers": [{"bookmaker": "fanduel", "legs": [
                    {"market": "fight_outcome", "name": "Ciryl Gane by KO/TKO", "price": 125,
                     "book_outcome_id": "734.19:31"},
                    {"market": "fight_outcome", "name": "Draw", "price": 5000, "book_outcome_id": "734.19:8"}]}]}):
            propline._CACHE.clear()
            offers = ufc_props.fight_book_offers(["Josh Hokit vs Ciryl Gane"])
        no = offers[("Josh Hokit vs Ciryl Gane", ("distance", "no"))]
        self.assertEqual(no["DraftKings"][0], -295)
        self.assertIn("outcomes=0QA%232", no["DraftKings"][1])
        self.assertIn("marketId[0]=734.19&selectionId[0]=76", no["FanDuel"][1])
        dec = offers[("Josh Hokit vs Ciryl Gane", ("mov", "ciryl gane", "DEC"))]
        self.assertEqual((dec["DraftKings"][0], dec["FanDuel"][0]), (350, 400))
        ko = offers[("Josh Hokit vs Ciryl Gane", ("mov", "ciryl gane", "KO"))]
        self.assertEqual(ko["FanDuel"][0], 125)
        self.assertIn("selectionId[0]=31", ko["FanDuel"][1])
        self.assertEqual(ufc_props.offer_key({"spec": {"kind": "distance"}, "yes": False}), ("distance", "no"))
        self.assertEqual(ufc_props.offer_key({"spec": {"kind": "mov", "method": "KO", "fighter": "X"}, "yes": True}),
                         ("mov", "x", "KO"))
        self.assertIsNone(ufc_props.offer_key({"spec": {"kind": "mov", "method": "KO", "fighter": "X"}, "yes": False}))
        self.assertIn("projections=16923664-u-57.5",
                      ufc_props.pickem_link("PrizePicks", "16923664", "under", 57.5))


class TotalStrikesTest(unittest.TestCase):
    def test_ratio_typed_line_and_grading_label(self):
        from linemaker import ufc_model, ufc_props
        cats = [{"displayName": "striking", "labels": ["TSL", "TSA", "SSL", "SSA"],
                 "statistics": [{"stats": ["60", "100", "40", "80"]}, {"stats": ["30", "50", "20", "40"]},
                                {"stats": ["-", "-", "-", "-"]}]}]
        self.assertEqual(ufc_model.total_ratio(cats), {"ts_ratio": 1.5, "ts_fights": 2})
        self.assertEqual(ufc_props.stat_prop_ask("hokit over 60.5 total strikes -120"),
                         {"over": True, "line": 60.5, "stat": "total strikes", "odds": -120})
        row = {"fighter": "A", "rounds": 3, "early": 0.4, "minutes": 11.0, "sig_strikes": 40.0, "total_strikes": 60.0}
        over = ufc_props.price_stat_prop(row, "total strikes", 50.5, True)
        self.assertGreater(over, ufc_props.price_stat_prop(row, "significant strikes", 50.5, True))
        self.assertEqual(ufc_model.STAT_LABEL["total strikes"], ("striking", "TSL"))


class StrikeLinksTest(unittest.TestCase):
    def _play(self, books, dfs=None, ids=None, links=None, keys=None, side="over", chance=0.6):
        prop = {"fighter": "Josh Hokit", "fight": ("Josh Hokit", "Ciryl Gane"), "stat": "significant strikes",
                "line": 45.5, "books": books, "dfs": dfs or {}, "mult": {}, "ids": ids or {},
                "links": links or {}, "keys": keys or {}}
        return {"prop": prop, "side": side, "chance": chance, "apps": {"prizepicks": 0.5425, "underdog": 0.5425}}

    def test_feed_slips_pickem_and_fight_pages(self):
        from linemaker import ufc_props
        r = self._play({"FanDuel": {"over": -115, "under": -115}}, dfs={"PrizePicks": {"over": 100}},
                       ids={"FanDuel": {"over": "734.1:9"}, "PrizePicks": {"over": "555"}}, keys={"FanDuel": "fanduel"})
        quotes, pages = ufc_props.strike_quotes(r, {})
        self.assertEqual([(t, o, g) for t, o, _u, g in quotes], [("FD", -115, True), ("PP", None, True)])
        self.assertIn("selectionId[0]=9", quotes[0][2])
        self.assertIn("projections=555-o-45.5", quotes[1][2])
        self.assertEqual(pages, [])
        # not in the feed at FanDuel / DraftKings: FanDuel's builder leg, else the fight's pages
        bare = self._play({}, dfs={"PrizePicks": {"over": 100}}, ids={"PrizePicks": {"over": "555"}})
        offers = {("Josh Hokit vs Ciryl Gane", ("page",)): {"DraftKings": (None, "https://dk/event/34")}}
        quotes, pages = ufc_props.strike_quotes(bare, offers)
        self.assertEqual([t for t, *_ in quotes], ["PP"])
        self.assertEqual(pages, [("DK", "https://dk/event/34")])
        offers[("Josh Hokit vs Ciryl Gane", ("stat", "josh hokit", "significant strikes", 45.5, "over"))] = {
            "FanDuel": (-120, "https://fd/slip")}
        quotes, pages = ufc_props.strike_quotes(bare, offers)
        self.assertEqual(quotes[0][:3], ("FD", -120, "https://fd/slip"))
        self.assertEqual(pages, [])

    def test_fanduel_builder_strike_legs_and_polymarket(self):
        from linemaker import propline, ufc_props
        odds = {"bookmakers": [
            {"key": "draftkings", "title": "DraftKings", "link": "https://sportsbook.draftkings.com/event/34", "markets": [
                {"key": "fight_distance", "outcomes": [{"name": "No", "price": -295, "book_outcome_id": "0QA#2"}]}]},
            {"key": "polymarket_us", "title": "Polymarket US", "link": "https://polymarket.com/event/ufc-gane", "markets": [
                {"key": "fight_distance", "outcomes": [{"name": "No", "price": -280}]}]}]}
        legs = {"bookmakers": [{"bookmaker": "fanduel", "legs": [
            {"market": "player_significant_strikes", "name": "Over", "description": "Josh Hokit", "point": 45.5,
             "price": -120, "book_outcome_id": "734.2:11"},
            {"market": "player_total_strikes", "name": "Under", "description": "Ciryl Gane", "point": 70.5,
             "price": -105, "book_outcome_id": "734.3:12"}]}]}
        with mock.patch.dict(os.environ, {"PROPLINE_API_KEY": "k"}), \
                mock.patch.object(propline, "events", return_value=[{"id": "1", "home_team": "Josh Hokit",
                                                                     "away_team": "Ciryl Gane"}]), \
                mock.patch.object(propline, "event_odds", return_value=odds), \
                mock.patch.object(propline, "_call", return_value=legs):
            propline._CACHE.clear()
            offers = ufc_props.fight_book_offers(["Josh Hokit vs Ciryl Gane"])
        f = "Josh Hokit vs Ciryl Gane"
        self.assertEqual(offers[(f, ("distance", "no"))]["Polymarket"],
                         (-280, "https://polymarket.com/event/ufc-gane"))
        self.assertEqual(offers[(f, ("page",))], {"DraftKings": (None, "https://sportsbook.draftkings.com/event/34")})
        self.assertEqual(offers[(f, ("stat", "josh hokit", "significant strikes", 45.5, "over"))]["FanDuel"][0], -120)
        self.assertIn((f, ("stat", "ciryl gane", "total strikes", 70.5, "under")), offers)

    def test_fair_buttons_share_the_25(self):
        from linemaker import ufc_props
        groups = [[(f"#{i} FD", "u"), (f"#{i} DK", "u"), (f"#{i} PM", "u")] for i in range(1, 11)] + [[("#11 PP", "u")]]
        out = ufc_props.fair_buttons(groups)
        self.assertEqual(len(out), 25)
        self.assertIn(("#11 PP", "u"), out)                       # the strike play still gets its button
        self.assertEqual(out[:3], [("#1 FD", "u"), ("#1 DK", "u"), ("#1 PM", "u")])   # play order kept

    def test_card_strike_row_lists_books_and_buttons(self):
        from linemaker import ufc_props
        r = self._play({"FanDuel": {"over": -115, "under": -115}}, dfs={"PrizePicks": {"over": 100}},
                       ids={"FanDuel": {"over": "734.1:9"}, "PrizePicks": {"over": "555"}}, keys={"FanDuel": "fanduel"})
        r.update(model=0.7, odds=-115, book="FanDuel", edge=0.065, flags=[], proj=52.0, row={"fighter": "Josh Hokit"})
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"LINEMAKER_TRACKING_DIR": d, "PROPLINE_API_KEY": "k"}), \
                mock.patch("linemaker.ufc_model.next_card_date", return_value="2026-11-14"), \
                mock.patch.object(ufc_props, "props_lines", return_value=([], [])), \
                mock.patch.object(ufc_props, "propline_edges", return_value=([r], 1)), \
                mock.patch.object(ufc_props, "fight_book_offers", return_value={}) as fbo:
            embeds, _text, buttons = ufc_props.props_card("2026-11-14")
        fbo.assert_called_once_with(["Josh Hokit vs Ciryl Gane"])
        strike = next(e for e in embeds if e.title.startswith("🥊 Strike"))
        self.assertIn("FD -115 ✅ · PP ✅", strike.description)
        self.assertEqual([b[0] for b in buttons], ["#1 FD -115", "#1 PP"])
