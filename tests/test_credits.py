"""Credit savers: no paid calls for started games, longshots or repeat questions."""
import datetime as dt
import os
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from linemaker import oddsapi
from tests.test_oddsapi import FakeAPI

TZ = ZoneInfo("America/New_York")


class CreditTest(unittest.TestCase):
    def test_started_games_get_no_prop_call(self):
        api = FakeAPI()
        with mock.patch.dict(os.environ, {"LINEMAKER_SKIP_STARTED": "on"}):
            oddsapi.fetch_slate(api, "NBA", "2026-10-21", TZ, props=True, game_lines=False)
        paid = [p for p, _ in api.calls if p.endswith("/odds")]
        start = dt.datetime(2026, 10, 21, 23, 30, tzinfo=dt.timezone.utc)
        if dt.datetime.now(dt.timezone.utc) >= start:
            self.assertEqual(paid, [])          # the fixture game has tipped: nothing bought
        else:
            self.assertEqual(len(paid), 1)

    def test_longshots_skipped_for_favorites_window(self):
        with mock.patch.dict(os.environ, {"LINEMAKER_ODDS_MIN": "-105", "LINEMAKER_ALT_LINES": "off"}):
            mlb = oddsapi.default_prop_markets("MLB")
            nhl = oddsapi.default_prop_markets("NHL")
        self.assertNotIn("batter_home_runs", mlb)
        self.assertNotIn("player_goal_scorer_anytime", nhl)
        self.assertIn("player_anytime_td", oddsapi.default_prop_markets("NFL"))
        with mock.patch.dict(os.environ, {"LINEMAKER_ODDS_MIN": ""}):
            self.assertIn("batter_home_runs", oddsapi.default_prop_markets("MLB"))
        with mock.patch.dict(os.environ, {"LINEMAKER_ODDS_MIN": "-105", "LINEMAKER_SKIP_LONGSHOTS": "off"}):
            self.assertIn("batter_home_runs", oddsapi.default_prop_markets("MLB"))

    def test_picks_reuses_the_chat_pull(self):
        from linemaker import chat
        apis = []

        def make():
            apis.append(FakeAPI())
            return apis[-1]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"LINEMAKER_CACHE_DIR": tmp}), \
                mock.patch.object(chat, "OddsAPI", make), \
                mock.patch.object(chat, "_SLATES", {}), \
                mock.patch.object(chat, "_today", lambda: "2026-10-21"), \
                mock.patch("linemaker.live.run_best") as run_best, \
                mock.patch("linemaker.live.run_games") as run_games:
            from linemaker.live import LiveRun
            from linemaker.pro import ProResult
            res = ProResult(slate_date="2026-10-21")
            res.projection_source = "ESPN game-log model"
            run_best.return_value = LiveRun(result=res, rows=[], feed_count=0, remaining=99)
            run_games.return_value = run_best.return_value
            chat.slate_for("NBA", "2026-10-21")
            chat.slate_for("NBA", "2026-10-21")            # same window: cached
            self.assertEqual(run_best.call_count, 1)
            chat._SLATES.clear()
            chat.slate_for("NBA", "2026-10-21")            # restart: read back from disk
            self.assertEqual(run_best.call_count, 1)
            chat.slate_for("NBA", "2026-10-21", refresh=True)   # "fresh odds": buys again
            self.assertEqual(run_best.call_count, 2)
            self.assertLess(chat.slate_age_minutes("NBA", "2026-10-21"), 1)
            run_best.call_count = 1
            chat.slate_for("NBA", "2026-10-21", "games")   # different bet kind: its own pull
            self.assertEqual((run_best.call_count, run_games.call_count), (1, 1))


class RefreshWordsTest(unittest.TestCase):
    def test_words(self):
        from linemaker.chat import REFRESH_WORDS
        from linemaker.scan_format import odds_age_note
        self.assertTrue(REFRESH_WORDS.search("best nba props, fresh odds"))
        self.assertTrue(REFRESH_WORDS.search("updated moneylines tonight"))
        self.assertFalse(REFRESH_WORDS.search("best nba props today"))
        res = type("R", (), {"odds_age": 12.4})()
        self.assertIn("12 min ago", odds_age_note([("NBA", res, {})]))
        res.odds_age = 0.2
        self.assertEqual(odds_age_note([("NBA", res, {})]), "Odds pulled just now")


if __name__ == "__main__":
    unittest.main()
