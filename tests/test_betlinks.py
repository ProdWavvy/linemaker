import asyncio
import json
import os
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from linemaker import betlinks
from linemaker.oddsapi import feed_row, parse_event_props
from linemaker.parlay import Parlay
from linemaker.pro import rows_to_markets

TZ = ZoneInfo("America/New_York")


def event(eid, player, fd_market, fd_sel, dk_out):
    def outcome(name, price, link="", sid=""):
        return {"name": name, "description": player, "price": price, "point": 14.5,
                "link": link, "sid": sid}
    return {"id": eid, "home_team": "Home", "away_team": "Away",
            "commence_time": "2026-10-21T23:30:00Z",
            "bookmakers": [
                {"key": "fanduel", "title": "FanDuel", "markets": [
                    {"key": "player_points", "sid": fd_market, "outcomes": [
                        outcome("Over", -400, f"https://{{state}}.sportsbook.fanduel.com/addToBetslip?"
                                              f"marketId[0]={fd_market}&selectionId[0]={fd_sel}"),
                        outcome("Under", 300)]}]},
                {"key": "draftkings", "title": "DraftKings", "markets": [
                    {"key": "player_points", "outcomes": [
                        outcome("Over", -380, sid=dk_out), outcome("Under", 280)]}]}]}


def market(eid, player, fd_market, fd_sel, dk_out):
    fm = parse_event_props(event(eid, player, fd_market, fd_sel, dk_out), "NBA", TZ)[0]
    row = feed_row(fm)
    row.update(mean="20", stdev="5")
    return rows_to_markets([json.loads(json.dumps(row))])[0]   # survives the JSON cache


class FakeEval:
    def __init__(self, m):
        self.market = m
        self.best = type("B", (), {"side": "A"})()


class BetLinksTest(unittest.TestCase):
    def setUp(self):
        self.a = market("e1", "Trae Young", "734.1", "111", "0QA1")
        self.b = market("e2", "Paolo Banchero", "734.2", "222", "0QA2")

    def test_links_ride_through_the_feed(self):
        self.assertEqual(set(self.a.links), {"FanDuel", "DraftKings"})
        self.assertIsNone(betlinks.leg(self.a, "B", "DraftKings"))   # Under had no link
        with mock.patch.dict(os.environ, {"LINEMAKER_STATE": "nj"}):
            self.assertEqual(betlinks.leg_link(self.a, "A", "FanDuel"),
                             "https://nj.sportsbook.fanduel.com/addToBetslip?marketId[0]=734.1&selectionId[0]=111")
        with mock.patch.dict(os.environ, {"LINEMAKER_STATE": ""}):
            self.assertTrue(betlinks.leg_link(self.a, "A", "FanDuel").startswith(
                "https://sportsbook.fanduel.com/addToBetslip?"))
        # DraftKings had only a sid: the link is built from it.
        self.assertEqual(betlinks.leg_link(self.a, "A", "DraftKings"),
                         "https://sportsbook.draftkings.com/event/e1?outcomes=0QA1")

    def test_parlay_links_combine_every_leg(self):
        with mock.patch.dict(os.environ, {"LINEMAKER_STATE": ""}):
            fd = betlinks.parlay_link(Parlay("FanDuel", [(FakeEval(self.a), -400), (FakeEval(self.b), -400)]))
            dk = betlinks.parlay_link(Parlay("DraftKings", [(FakeEval(self.a), -380), (FakeEval(self.b), -380)]))
        self.assertEqual(fd, "https://sportsbook.fanduel.com/addToBetslip?marketId[0]=734.1&selectionId[0]=111"
                             "&marketId[1]=734.2&selectionId[1]=222")
        self.assertEqual(dk, "https://sportsbook.draftkings.com/event/e1?outcomes=0QA1+0QA2")

    def test_missing_leg_means_no_link(self):
        self.b.links = {}
        self.assertIsNone(betlinks.parlay_link(Parlay("FanDuel", [(FakeEval(self.a), -400),
                                                                  (FakeEval(self.b), -400)])))

    def test_request_asks_for_links(self):
        from tests.test_oddsapi import FakeAPI
        api = FakeAPI()
        api.event_odds("NBA", "evt1", "player_points")
        self.assertEqual(api.calls[-1][1].get("includeLinks"), "true")
        with mock.patch.dict(os.environ, {"LINEMAKER_BET_LINKS": "off"}):
            api.event_odds("NBA", "evt1", "player_points")
        self.assertNotIn("includeLinks", api.calls[-1][1])

    def test_buttons(self):
        from linemaker import bot

        async def build():
            pick = bot.PickView("k1", {"ev": object(), "links": [("FanDuel", "https://x.test/a")]})
            slip = bot.PickView("k2", {"links": [("2-leg on DraftKings", "https://x.test/b"),
                                                 ("too long", "https://x.test/" + "a" * 600)]})
            return pick, slip
        pick, slip = asyncio.run(build())
        urls = [c.url for c in pick.children if getattr(c, "url", None)]
        self.assertEqual(urls, ["https://x.test/a"])
        self.assertEqual([c.label for c in pick.children], ["Bet on FanDuel", "I bet this", "Stats"])   # one row
        self.assertEqual([c.label for c in slip.children], ["2-leg on DraftKings"])


if __name__ == "__main__":
    unittest.main()
