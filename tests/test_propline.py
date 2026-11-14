import io
import json
import os
import unittest
import urllib.error
from unittest import mock

from linemaker import propline, ufc_props


class FakeResp(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(json.dumps(body).encode())
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ClientTest(unittest.TestCase):
    def setUp(self):
        propline._CACHE.clear()
        propline._QUOTA.clear()

    def test_key_header_quota_and_cache(self):
        calls = []

        def opener(req, timeout=0):
            calls.append(req)
            return FakeResp([{"id": "1", "home_team": "A", "away_team": "B"}],
                            {"X-Daily-Used": "12", "X-Daily-Limit": "5000"})
        with mock.patch.dict(os.environ, {"PROPLINE_API_KEY": "k"}), \
                mock.patch.object(propline.urllib.request, "urlopen", opener):
            self.assertEqual(len(propline.events("UFC")), 1)
            propline.events("UFC")                                  # cached
        self.assertEqual(len(calls), 1)
        self.assertIn("/sports/mma_ufc/events", calls[0].full_url)
        self.assertEqual(calls[0].get_header("X-api-key"), "k")
        self.assertIn("12 of 5000", propline.usage_line())

    def test_errors_carry_the_code(self):
        def opener(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {"X-Daily-Used": "3"},
                                         io.BytesIO(b'{"detail": {"error": "upgrade_required", "message": "Hobby+"}}'))
        with mock.patch.dict(os.environ, {"PROPLINE_API_KEY": "k"}):
            with self.assertRaises(propline.PropLineError) as cm:
                propline._call("/x", opener=opener)
        self.assertEqual(cm.exception.code, "upgrade_required")

    def test_no_key_no_call(self):
        with mock.patch.dict(os.environ, {"PROPLINE_API_KEY": ""}):
            self.assertFalse(propline.on())
            lines, note = ufc_props.propline_lines("2026-11-14")
        self.assertEqual(lines, [])
        self.assertIn("screenshot", note)

    def test_find_event_either_way_round(self):
        evs = [{"id": "9", "home_team": "Boston Celtics", "away_team": "New York Knicks",
                "commence_time": "2026-11-15T00:30:00Z"}]
        with mock.patch.object(propline, "events", return_value=evs):
            self.assertEqual(propline.find_event("NBA", "New York Knicks", "Boston Celtics",
                                                 "2026-11-15T00:40:00Z")["id"], "9")
            self.assertIsNone(propline.find_event("NBA", "New York Knicks", "Boston Celtics",
                                                  "2026-11-17T00:40:00Z"))


ODDS = {"bookmakers": [
    {"key": "fanduel", "title": "FanDuel", "markets": [
        {"key": "player_significant_strikes", "outcomes": [
            {"name": "Over", "description": "Juan Diaz", "price": -115, "point": 45.5},
            {"name": "Under", "description": "Juan Diaz", "price": -115, "point": 45.5}]},
        {"key": "fight_time_minutes", "outcomes": [
            {"name": "Over", "description": "", "price": -120, "point": 9.5},
            {"name": "Under", "description": "", "price": -110, "point": 9.5}]}]},
    {"key": "prizepicks", "title": "PrizePicks", "markets": [
        {"key": "player_takedowns", "outcomes": [
            {"name": "Over", "description": "Juan Diaz", "price": 100, "point": 2.5},
            {"name": "Under", "description": "Juan Diaz", "price": 100, "point": 2.5}]}]}]}
ROWS = [{"fighter": "Juan Diaz", "opponent": "Alatengheili", "rounds": 3, "early": 0.3, "minutes": 12.5,
         "sig_strikes": 70.0, "takedowns": 8.0, "stat_fights": 18},
        {"fighter": "Alatengheili", "opponent": "Juan Diaz", "rounds": 3, "early": 0.3, "minutes": 12.5,
         "sig_strikes": 30.0, "takedowns": 1.3, "stat_fights": 30}]


class UfcLinesTest(unittest.TestCase):
    def test_parse_groups_books_and_pickem(self):
        props = ufc_props.propline_props([{"id": "e", "home_team": "Alatengheili", "away_team": "Juan Diaz"}],
                                         lambda eid: ODDS)
        ss = next(p for p in props if p["stat"] == "significant strikes")
        self.assertEqual(ss["books"]["FanDuel"], {"over": -115, "under": -115})
        self.assertAlmostEqual(ss["fair_over"], 0.5)
        td = next(p for p in props if p["stat"] == "takedowns")
        self.assertIn("PrizePicks", td["dfs"])
        self.assertIsNone(td["fair_over"])                          # pick'em only: no two-way price
        ft = next(p for p in props if p["stat"] == "fight time")
        self.assertEqual(ft["fighter"], "Alatengheili")             # fight-level: listed under the event

    def test_edges_and_lines(self):
        with mock.patch.dict(os.environ, {"PROPLINE_API_KEY": "k"}), \
                mock.patch.object(propline, "events", return_value=[{"id": "e", "home_team": "Alatengheili",
                                                                     "away_team": "Juan Diaz"}]), \
                mock.patch.object(propline, "event_odds", return_value=ODDS), \
                mock.patch("linemaker.ufc_model.fighter_props", return_value=ROWS), \
                mock.patch("linemaker.ufc_model.next_card_date", return_value="2026-11-14"), \
                mock.patch("linemaker.pro.parlay_books", return_value=("FanDuel", "DraftKings")):
            edges, seen = ufc_props.propline_edges("2026-11-14", min_edge=0.0)
            lines, note = ufc_props.propline_lines("2026-11-14")
        self.assertEqual(seen, 3)
        ss = next(e for e in edges if e["prop"]["stat"] == "significant strikes")
        self.assertEqual((ss["side"], ss["book"], ss["odds"]), ("over", "FanDuel", -115))
        td = next(e for e in edges if e["prop"]["stat"] == "takedowns")
        self.assertIsNone(td["odds"])                                # priced against the pick'em break-even
        self.assertTrue(any("Over 9.5 fight time** -120 FanDuel" in l for l in lines))   # 3+ pt edge shown
        self.assertFalse(any("sig strikes" in l for l in lines))                         # 1 pt edge isn't


def _ev(bet_type, subject, opponent, market, line, side, game_id="g1"):
    from types import SimpleNamespace as NS
    m = NS(bet_type=bet_type, subject=subject, opponent=opponent, market=market, line=line, sport="NBA",
           game_id=game_id, game="New York Knicks @ Boston Celtics", commence="2026-11-15T00:30:00Z",
           links={"FanDuel": [{"mk": "player_points_alternate", "link": "x"}, {"mk": "player_points_alternate", "link": "y"}]},
           side_keys=("A", "B"))
    return NS(market=m, best=NS(side=side, label=f"{subject}"))


class SgpTest(unittest.TestCase):
    def test_leg_forms(self):
        from linemaker.parlay import sgp_leg
        self.assertEqual(sgp_leg(_ev("prop", "Jalen Brunson", "", "points", 24.5, "A"), "FanDuel"),
                         {"market": "player_points", "name": "Over", "description": "Jalen Brunson", "point": 24.5})
        self.assertEqual(sgp_leg(_ev("spread", "Boston Celtics", "New York Knicks", "spread", -5.5, "B"), "FanDuel"),
                         {"market": "spreads", "name": "New York Knicks", "point": 5.5})
        self.assertEqual(sgp_leg(_ev("moneyline", "Boston Celtics", "New York Knicks", "moneyline", 0, "A"), "FanDuel"),
                         {"market": "h2h", "name": "Boston Celtics"})
        self.assertEqual(sgp_leg(_ev("total", "Boston Celtics", "New York Knicks", "game total", 221.5, "B"), "FanDuel"),
                         {"market": "totals", "name": "Under", "point": 221.5})

    def test_refine_uses_the_books_price_and_drops_refused(self):
        from types import SimpleNamespace as NS
        from linemaker import parlay
        a = _ev("prop", "Jalen Brunson", "", "points", 24.5, "A")
        b = _ev("prop", "Jayson Tatum", "", "points", 26.5, "A")
        c = _ev("prop", "Derrick White", "", "points", 12.5, "B", game_id="g2")
        good = parlay.Parlay(book="FanDuel", legs=[(a, -150), (b, -140), (c, -200)], kind="SGP+", decimal=3.0)
        bad = parlay.Parlay(book="DraftKings", legs=[(a, -150), (b, -140)], kind="SGP", decimal=2.5)
        plain = parlay.Parlay(book="FanDuel", legs=[(a, -150), (c, -200)], decimal=2.0)
        quotes = {"fanduel": {"quoted": True, "sgp_price": 180, "sgp_price_decimal": 2.8},
                  "draftkings": {"quoted": False}}
        with mock.patch.dict(os.environ, {"PROPLINE_API_KEY": "k"}), \
                mock.patch.object(propline, "find_event", return_value={"id": "9"}), \
                mock.patch.object(propline, "sgp_quote", side_effect=lambda s, e, legs, book: quotes[book]) as q:
            out = parlay.refine_sgp_prices([good, bad, plain])
        self.assertEqual(out, [good, plain])                         # DraftKings won't take its pair
        self.assertAlmostEqual(good.decimal, 2.8 * 1.5)              # the SGP group at FanDuel's price x the other game
        self.assertTrue(good.sgp_live)
        self.assertEqual(q.call_count, 2)
        from linemaker.parlay_format import sgp_line
        self.assertIn("own same-game price", sgp_line(good))

    def test_no_key_keeps_estimates(self):
        from linemaker import parlay
        p = parlay.Parlay(book="FanDuel", legs=[], kind="SGP", decimal=2.5)
        with mock.patch.dict(os.environ, {"PROPLINE_API_KEY": ""}):
            self.assertEqual(parlay.refine_sgp_prices([p]), [p])
        self.assertEqual(p.decimal, 2.5)


class CompareTest(unittest.TestCase):
    def test_compare_and_freshness(self):
        def ev(prices):
            return {"bookmakers": [{"key": "fanduel", "markets": [{"key": "player_points", "outcomes": [
                {"name": n, "description": "Jalen Brunson", "price": p, "point": 24.5} for n, p in prices]}]}]}
        out = propline.compare(ev([("Over", -115), ("Under", -105)]), ev([("Over", -120), ("Under", -105)]))
        self.assertEqual((out["matched"], out["same"]), (2, 1))
        self.assertEqual(out["by_book"]["fanduel"], (2, 1))
        self.assertEqual(out["worst"][0][1:], (-115, -120))
        rows = {"bookmakers": [{"key": "fanduel", "market_classes": {"props": {"staleness_seconds": 125, "is_stale": False}}},
                               {"key": "bovada", "staleness_seconds": 5}]}
        self.assertEqual(propline.freshness_lines(rows), ["fanduel: props 2 min old"])
