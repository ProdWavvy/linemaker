import asyncio
import os
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from linemaker import pp

TZ = ZoneInfo("America/New_York")


class MathTest(unittest.TestCase):
    def test_tables_and_break_even(self):
        self.assertEqual(pp.power_table()[2], 3.0)
        self.assertEqual(pp.flex_table()[5], [10.0, 2.0, 0.4])
        self.assertAlmostEqual(pp.break_even(), 3 ** -0.5)       # 2-pick Power: 57.7%
        with mock.patch.dict(os.environ, {"LINEMAKER_PP_POWER": "2:3.5,3:6"}):
            self.assertAlmostEqual(pp.power_table()[3], 6.0)

    def test_hits_distribution(self):
        d = pp.hits_distribution([0.6, 0.6, 0.6])
        self.assertAlmostEqual(sum(d), 1.0)
        self.assertAlmostEqual(d[3], 0.216)
        self.assertAlmostEqual(d[2], 3 * 0.36 * 0.4)

    def test_settle(self):
        bet = {"stake": 10, "pp_kind": "Power"}
        self.assertEqual(pp.settle_entry(bet, ["won", "won", "won"]), ("won", 40.0))      # 3-pick 5x
        self.assertEqual(pp.settle_entry(bet, ["won", "lost", "won"]), ("lost", -10.0))
        self.assertEqual(pp.settle_entry(bet, ["won", "push", "won"]), ("won", 20.0))     # drops to 2-pick 3x
        self.assertIsNone(pp.settle_entry(bet, ["won", None, "won"]))
        flex = {"stake": 10, "pp_kind": "Flex"}
        self.assertEqual(pp.settle_entry(flex, ["won", "won", "won", "lost"]), ("won", 5.0))   # 4-pick 3/4: 1.5x
        self.assertEqual(pp.settle_entry(flex, ["won", "lost", "lost", "won"]), ("lost", -10.0))


def event(eid, home, away, players):
    def o(name, player, point, price=-119):
        return {"name": name, "description": player, "price": price, "point": point}
    return {"id": eid, "home_team": home, "away_team": away, "commence_time": "2026-10-21T23:30:00Z",
            "bookmakers": [{"key": "prizepicks", "title": "PrizePicks", "markets": [
                {"key": "player_points", "outcomes": [x for p, line in players
                                                      for x in (o("Over", p, line), o("Under", p, line))]}]}]}


class FakePP:
    """Stand-in Odds API: two games of PrizePicks lines, free /events."""
    def __init__(self):
        from linemaker.oddsapi import Quota
        self.quota, self.slate, self.calls = Quota(), {}, []
        self.events_ = [event("e1", "Home Hawks", "Road Bulls", [("Star Guard", 22.5)]),
                        event("e2", "H2", "A2", [("Big Man", 10.5)])]

    def events(self, sport, start, end):
        return [{k: v for k, v in e.items() if k != "bookmakers"} for e in self.events_]

    def event_odds(self, sport, eid, markets):
        self.calls.append(eid)
        return next(e for e in self.events_ if e["id"] == eid)


class EndToEndTest(unittest.TestCase):
    def test_pp_board_entries_and_buttons(self):
        from linemaker import bot
        from linemaker.pro import rows_to_markets, run_pro
        from linemaker.scan_format import pp_messages
        # The props slate: projections, plus the sportsbooks' prices at the main lines.
        slate = [{"sport": "NBA", "subject": "Star Guard", "market": "points", "line": "24.5", "mean": "27",
                  "stdev": "5", "books": "FanDuel:-110/-110|DraftKings:-115/-105"},
                 {"sport": "NBA", "subject": "Big Man", "market": "points", "line": "10.5", "mean": "14",
                  "stdev": "3", "books": "FanDuel:-120/+100|DraftKings:-118/-102"}]
        with mock.patch.dict(os.environ, {"LINEMAKER_SKIP_STARTED": "off"}):
            rows, feed = pp.run_pp(FakePP(), "NBA", "2026-10-21", TZ, slate)
        self.assertEqual(len(rows), 2)
        star = next(r for r in rows if r["subject"] == "Star Guard")
        self.assertIn("books' line 24.5", star["notes"])           # PP 22.5 vs books 24.5
        big = next(r for r in rows if r["subject"] == "Big Man")
        self.assertIn("FanDuel", big["books"])                     # same number: books anchor it
        cfg = pp.pp_config()
        res = run_pro(rows_to_markets(rows), "2026-10-21", cfg)
        res.picks = pp.select_legs(res.picks, cfg.pp_floor)
        self.assertEqual(len(res.picks), 2)
        self.assertTrue(all(ev.best.blended >= pp.break_even() for ev in res.picks))
        self.assertAlmostEqual(pp.line_gap(next(e for e in res.picks if e.market.subject == "Star Guard")), 2.0)
        res.kind = "pp"
        messages = pp_messages([("NBA", res, {"profiles": {}, "context": {}, "history": {}})], "2026-10-21")
        titles = [m[0][0].title for m in messages]
        self.assertTrue(titles[0].startswith("🟣"))
        self.assertIn("✅ PrizePicks Legs", titles)
        self.assertIn("🎟️ Best Entries", titles)

        async def build():
            return bot.compact_card(messages)
        embeds, view = asyncio.run(build())
        labels = [getattr(c, "label", "") for c in view.children]
        self.assertIn("Open PrizePicks", labels)                   # no API links in this fixture
        self.assertTrue(any(l.startswith("Log 2-pick Power") for l in labels))

    def test_phrases(self):
        from linemaker.chat import quick_kind, quick_plays_request
        self.assertEqual(quick_kind("best prizepicks nba"), "pp")
        self.assertEqual(quick_kind("best pp plays tonight"), "pp")
        self.assertEqual(quick_plays_request("prizepicks nba"), ["NBA"])
        self.assertEqual(quick_kind("best nba props"), "props")


if __name__ == "__main__":
    unittest.main()


def alt_event(eid, home, away, lines):
    """PrizePicks alternate market: goblins / demons, More only, with multipliers."""
    return {"id": eid, "home_team": home, "away_team": away, "commence_time": "2026-10-21T23:30:00Z",
            "bookmakers": [{"key": "prizepicks", "title": "PrizePicks", "markets": [
                {"key": "player_points_alternate", "outcomes": [
                    {"name": "Over", "description": p, "price": -119, "point": line, "multiplier": m}
                    for p, line, m in lines]}]}]}


class FakePPAlt(FakePP):
    def event_odds(self, sport, eid, markets):
        self.calls.append((eid, markets))
        if "alternate" in markets:
            return {"e1": alt_event("e1", "Home Hawks", "Road Bulls",
                                    [("Star Guard", 17.5, 0.5), ("Star Guard", 29.5, 2.0)]),
                    "e2": alt_event("e2", "H2", "A2", [("Big Man", 12.5, 1.5)])}[eid]
        return super().event_odds(sport, eid, markets)


class GoblinDemonTest(unittest.TestCase):
    slate = [{"sport": "NBA", "subject": "Star Guard", "market": "points", "line": "24.5", "mean": "27",
              "stdev": "5", "books": "FanDuel:-110/-110|DraftKings:-115/-105"},
             {"sport": "NBA", "subject": "Big Man", "market": "points", "line": "10.5", "mean": "14",
              "stdev": "3", "books": "FanDuel:-120/+100|DraftKings:-118/-102"}]

    def legs(self, env=None):
        from linemaker.pro import rows_to_markets, run_pro
        with mock.patch.dict(os.environ, dict({"LINEMAKER_SKIP_STARTED": "off"}, **(env or {}))):
            api = FakePPAlt()
            rows, feed = pp.run_pp(api, "NBA", "2026-10-21", TZ, self.slate)
            cfg = pp.pp_config()
            res = run_pro(rows_to_markets(rows), "2026-10-21", cfg)
        return rows, res, pp.select_legs(res.picks, cfg.pp_floor), api

    def test_multipliers_flow_and_value_ranking(self):
        rows, res, legs, api = self.legs()
        self.assertTrue(api.include_multipliers)
        mults = sorted(float(r["pp_mult"]) for r in rows)
        self.assertEqual(mults, [0.5, 1.0, 1.0, 1.5, 2.0])
        demon = next(ev for ev in res.evaluated if ev.market.multiplier == 1.5)
        self.assertEqual(pp.tier(demon), "demon")
        self.assertEqual(demon.best.side, "A")                              # More only
        # one line per player-stat, chosen by hit chance x multiplier
        self.assertEqual(len({(e.market.subject, e.market.market) for e in legs}), len(legs))
        for ev in legs:
            same = [e for e in res.picks if e.market.subject == ev.market.subject]
            self.assertEqual(pp.leg_value(ev), max(pp.leg_value(e) for e in same))
        big = next(e for e in legs if e.market.subject == "Big Man")
        self.assertEqual(big.market.multiplier, 1.5)                         # 14 avg vs 12.5 at 1.5x wins

    def test_entry_payout_scales_with_multipliers(self):
        _rows, _res, legs, _api = self.legs()
        e = pp.price_entry("Power", legs[:2])
        scale = pp.leg_mult(legs[0]) * pp.leg_mult(legs[1])
        self.assertAlmostEqual(e.payouts[0], 3.0 * scale)
        bet = {"stake": 10, "pp_kind": "Power", "legs": [{"pp_mult": 1.5}, {"pp_mult": 1.0}]}
        self.assertEqual(pp.settle_entry(bet, ["won", "won"]), ("won", 35.0))   # 3x * 1.5 = 4.5x

    def test_switches_and_fallbacks(self):
        rows, *_ = self.legs({"LINEMAKER_PP_DEMONS": "off"})
        self.assertNotIn(2.0, [float(r["pp_mult"]) for r in rows])
        self.assertNotIn(1.5, [float(r["pp_mult"]) for r in rows])
        rows, *_ = self.legs({"LINEMAKER_PP_GOBLINS": "off", "LINEMAKER_PP_DEMONS": "off"})
        self.assertEqual({float(r["pp_mult"]) for r in rows}, {1.0})


class SettleFlowTest(unittest.TestCase):
    def test_logged_entry_settles_from_pp_tracking(self):
        import json
        import tempfile
        from linemaker import bets
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LINEMAKER_TRACKING_DIR": d}):
            os.makedirs(os.path.join(d, "pp"))
            with open(os.path.join(d, "pp", "2026-10-21.json"), "w") as f:
                json.dump({"k1": {"result": {"winner": "A"}}, "k2": {"result": {"winner": "B"}}}, f)
            legs = [{"key": "k1", "side": "A", "date": "2026-10-21", "folder": "pp", "label": "a", "odds": 100},
                    {"key": "k2", "side": "B", "date": "2026-10-21", "folder": "pp", "label": "b", "odds": 100}]
            bets.add_bet(legs, 200, 5, "PrizePicks", kind="pp", pp_kind="Power")
            self.assertEqual(bets.settle(), 1)
            b = bets.load_bets()[0]
        self.assertEqual((b["status"], b["profit"]), ("won", 10.0))      # 2-pick Power 3x on $5


class NoMultiplierTest(unittest.TestCase):
    def test_skipped_unless_env_fallback(self):
        class NoMult(FakePPAlt):
            def event_odds(self, sport, eid, markets):
                d = super().event_odds(sport, eid, markets)
                for bk in d["bookmakers"]:
                    for mk in bk["markets"]:
                        for o in mk["outcomes"]:
                            o.pop("multiplier", None)
                return d
        slate = GoblinDemonTest.slate
        with mock.patch.dict(os.environ, {"LINEMAKER_SKIP_STARTED": "off"}):
            rows, _ = pp.run_pp(NoMult(), "NBA", "2026-10-21", TZ, slate)
        self.assertEqual({float(r["pp_mult"]) for r in rows}, {1.0})            # unknown payout: skipped
        with mock.patch.dict(os.environ, {"LINEMAKER_SKIP_STARTED": "off", "LINEMAKER_PP_GOBLIN_MULT": "0.75",
                                          "LINEMAKER_PP_DEMON_MULT": "1.6"}):
            rows, _ = pp.run_pp(NoMult(), "NBA", "2026-10-21", TZ, slate)
        got = sorted(float(r["pp_mult"]) for r in rows)
        self.assertIn(0.75, got)                                                 # 17.5 < 22.5: goblin
        self.assertIn(1.6, got)                                                  # 29.5 > 22.5: demon
