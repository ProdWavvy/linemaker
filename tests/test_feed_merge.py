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
