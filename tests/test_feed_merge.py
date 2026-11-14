import os
import unittest
from unittest import mock

from linemaker import feed_merge
from linemaker.oddsapi import FeedMarket


def fm(subject, line, books, game="g1", market="points"):
    return FeedMarket(date="2026-11-15", sport="NBA", game_id=game, game="A @ B", commence="", bet_type="prop",
                      subject=subject, opponent="", market=market, line=line, books=[(b, list(p)) for b, p in books])


def board(n, dk_price=-110, dk_over=None):
    return [fm(f"Player {i}", 20.5, [("DraftKings", [dk_over or dk_price, -110]), ("FanDuel", [-115, -105])])
            for i in range(n)]


class MergeTest(unittest.TestCase):
    def test_adds_books_and_takes_fresher_prices_when_feeds_agree(self):
        primary = board(25)
        extra = board(25)
        for m in extra:
            m.books.append(("Pinnacle", [-108, -112]))
        extra[0].books[0] = ("DraftKings", [-125, +105])            # DK moved; PropLine saw it first
        stats = feed_merge.merge(primary, extra, "fresh", "NBA props")
        self.assertEqual(stats["books_added"], 25)
        self.assertTrue(stats["applied_fresh"])
        self.assertEqual(primary[0].books[0], ("DraftKings", [-125, 105]))
        self.assertIn("Pinnacle", [b for b, _p in primary[3].books])

    def test_keeps_odds_api_prices_when_feeds_disagree(self):
        primary = board(25)
        extra = board(25, dk_over=+150)                              # every DK price differs a lot
        stats = feed_merge.merge(primary, extra, "fresh", "NBA props")
        self.assertFalse(stats["applied_fresh"])
        self.assertIn("kept The Odds API prices", stats["why"])
        self.assertEqual(primary[0].books[0][1][0], -110)

    def test_add_mode_never_swaps_and_new_lines_are_ignored(self):
        primary = board(25)
        extra = board(25)
        extra[0].books[0] = ("DraftKings", [-125, +105])
        extra.append(fm("Player 0", 30.5, [("DraftKings", [+200, -250])]))   # an alt rung the Odds API didn't pull
        stats = feed_merge.merge(primary, extra, "add", "NBA props")
        self.assertEqual(primary[0].books[0][1][0], -110)
        self.assertEqual(len(primary), 25)
        self.assertEqual(stats["refreshed"], 0)

    def test_book_names_line_up(self):
        self.assertEqual(feed_merge.same_book("Caesars Sportsbook"), feed_merge.same_book("williamhill_us"))
        self.assertEqual(feed_merge.same_book("Hard Rock Bet"), feed_merge.same_book("hardrockbet"))

    def test_mode(self):
        with mock.patch.dict(os.environ, {"PROPLINE_API_KEY": ""}):
            self.assertEqual(feed_merge.mode(), "off")
        with mock.patch.dict(os.environ, {"PROPLINE_API_KEY": "k", "LINEMAKER_PROPLINE_MERGE": "add"}):
            self.assertEqual(feed_merge.mode(), "add")

    def test_relabels_to_odds_api_ids(self):
        out = feed_merge._as_odds_api({"id": "999", "bookmakers": []},
                                      {"id": "abc", "commence_time": "t", "home_team": "H", "away_team": "A"})
        self.assertEqual((out["id"], out["home_team"]), ("abc", "H"))


PL_EVENT = {"id": "999", "home_team": "Cleveland Cavaliers", "away_team": "Dallas Mavericks",
            "commence_time": "2026-11-15T00:30:00Z", "bookmakers": [
                {"key": "fanduel", "title": "FanDuel", "link": "https://sportsbook.fanduel.com/x", "markets": [
                    {"key": "player_points", "line_type": "main", "outcomes": [
                        {"name": "Over", "description": "Cooper Flagg", "price": -113, "point": 24.5,
                         "book_outcome_id": "734.1:75"},
                        {"name": "Under", "description": "Cooper Flagg", "price": -107, "point": 24.5,
                         "book_outcome_id": "734.1:76"}]},
                    {"key": "player_points", "line_type": "alternate", "outcomes": [
                        {"name": "Over", "description": "Cooper Flagg", "price": 150, "point": 29.5}]}]},
                {"key": "draftkings", "title": "DraftKings", "link": "https://sportsbook.draftkings.com/event/35", "markets": [
                    {"key": "player_points", "line_type": "milestone", "outcomes": [
                        {"name": "25+ Points", "description": "Cooper Flagg", "price": 110}]},
                    {"key": "player_points", "line_type": "main", "outcomes": [
                        {"name": "Over", "description": "Cooper Flagg", "price": -110, "point": 24.5,
                         "book_outcome_id": "0QA#1"},
                        {"name": "Under", "description": "Cooper Flagg", "price": -110, "point": 24.5,
                         "book_outcome_id": "0QA#2"}]}]}]}
OA_EVENT = {"id": "oa1", "home_team": "Cleveland Cavaliers", "away_team": "Dallas Mavericks",
            "commence_time": "2026-11-15T00:30:00Z"}


class CreditSaverTest(unittest.TestCase):
    def test_props_in_odds_api_shape_with_slip_links(self):
        from zoneinfo import ZoneInfo
        from linemaker.oddsapi import parse_event_props
        rows = parse_event_props(feed_merge.as_odds_api_props(PL_EVENT, OA_EVENT), "NBA", ZoneInfo("America/New_York"))
        self.assertEqual(len(rows), 1)                               # main line only: no alt, no milestone
        m = rows[0]
        self.assertEqual(m.game_id, "oa1")
        self.assertEqual(dict(m.books)["FanDuel"], [-113, -107])
        from linemaker.betlinks import combine
        fd = combine("FanDuel", [m.links["FanDuel"][0]])
        self.assertIn("marketId[0]=734.1&selectionId[0]=75", fd)
        dk = combine("DraftKings", [m.links["DraftKings"][0]], "oa1")
        self.assertIn("sportsbook.draftkings.com/event/35", dk)
        self.assertIn("outcomes=0QA%231", dk)
        alt = feed_merge.as_odds_api_props(PL_EVENT, OA_EVENT, alt=True)
        keys = [mk["key"] for bk in alt["bookmakers"] for mk in bk["markets"]]
        self.assertIn("player_points_alternate", keys)

    def test_source_follows_the_daily_check(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"PROPLINE_API_KEY": "k", "LINEMAKER_CACHE_DIR": d,
                                             "LINEMAKER_PROPS_SOURCE": ""}), \
                mock.patch.object(feed_merge, "_today", return_value="2026-11-14"):
            self.assertEqual(feed_merge.props_source(), "propline")
            feed_merge._save("propline_check.json", {"date": "2026-11-14", "ok": False})
            self.assertEqual(feed_merge.props_source(), "oddsapi")
            feed_merge._save("propline_check.json", {"date": "2026-11-13", "ok": False})   # yesterday's
            self.assertEqual(feed_merge.props_source(), "propline")
        with mock.patch.dict(os.environ, {"PROPLINE_API_KEY": "k", "LINEMAKER_PROPS_SOURCE": "oddsapi"}):
            self.assertEqual(feed_merge.props_source(), "oddsapi")

    def test_daily_check_verdicts(self):
        import tempfile
        from linemaker import propline

        def board(price):
            return {"bookmakers": [{"key": "fanduel", "markets": [{"key": "player_points", "outcomes": [
                {"name": "Over", "description": f"P{i}", "price": price, "point": 20.5} for i in range(12)]}]}]}
        api = mock.Mock()
        api.event_odds.return_value = board(-110)
        ev = dict(OA_EVENT, commence_time="2099-01-01T00:00:00Z")
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LINEMAKER_CACHE_DIR": d}), \
                mock.patch.object(feed_merge, "_today", return_value="2026-11-14"), \
                mock.patch.object(propline, "find_event", return_value={"id": "9"}):
            with mock.patch.object(propline, "event_odds", return_value=board(-110)):
                self.assertTrue(feed_merge.run_daily_check(api, "NBA", [ev], "player_points")["ok"])
            with mock.patch.object(propline, "event_odds", return_value=board(+250)):
                self.assertFalse(feed_merge.run_daily_check(api, "NBA", [ev], "player_points")["ok"])
            self.assertIn("feeds disagree", feed_merge.check_line())

    def test_saved_counter(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LINEMAKER_CACHE_DIR": d}), \
                mock.patch.object(feed_merge, "_today", return_value="2026-11-14"):
            feed_merge.record_saved(40)
            feed_merge.record_saved(10)
            self.assertIn("~50 Odds API credits saved today", feed_merge.saved_line())

    def test_pickem_and_live_stay_on_the_odds_api(self):
        from zoneinfo import ZoneInfo
        from linemaker import oddsapi
        api = mock.Mock()
        api.include_multipliers = True
        api.slate = {}
        api.events.return_value = [dict(OA_EVENT, commence_time="2099-01-01T00:00:00Z")]
        api.event_odds.return_value = dict(OA_EVENT, commence_time="2099-01-01T00:00:00Z", bookmakers=[])
        with mock.patch.dict(os.environ, {"PROPLINE_API_KEY": "k"}), \
                mock.patch.object(feed_merge, "saver_props", side_effect=AssertionError("pick'em went to PropLine")), \
                mock.patch.object(oddsapi, "_local_date", return_value="2098-12-31"):
            oddsapi.fetch_slate(api, "NBA", "2098-12-31", ZoneInfo("America/New_York"), props=True, game_lines=False)
        api.event_odds.assert_called()
