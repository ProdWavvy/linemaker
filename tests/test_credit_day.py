import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock

from linemaker import oddsapi


class CreditDayTest(unittest.TestCase):
    def test_local_day_and_breakdown(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"LINEMAKER_CACHE_DIR": d, "LINEMAKER_TZ": "America/New_York"}):
            day = oddsapi.local_today()
            oddsapi.record_spend(30)                          # default source
            tok = oddsapi.SPEND_SOURCE.set("chat asks")
            oddsapi.record_spend(12)
            oddsapi.SPEND_SOURCE.reset(tok)
            self.assertEqual(oddsapi.credits_today(), 42)
            self.assertEqual(oddsapi.spend_breakdown(), {"buttons & commands": 30, "chat asks": 12})
            self.assertTrue(os.path.exists(os.path.join(d, f"credits_{day}.json")))
            # files from before the breakdown (a bare number) still read
            with open(os.path.join(d, "credits_2026-01-01.json"), "w") as f:
                json.dump(500, f)
            self.assertEqual(oddsapi.credits_today("2026-01-01"), 500)

    def test_day_follows_your_timezone_not_utc(self):
        late = dt.datetime(2026, 10, 26, 2, 30, tzinfo=dt.timezone.utc)    # 10:30 PM Oct 25 in New York
        with mock.patch.dict(os.environ, {"LINEMAKER_TZ": "America/New_York"}), \
                mock.patch.object(oddsapi.dt, "datetime", wraps=dt.datetime) as fake:
            fake.now.side_effect = lambda tz=None: late.astimezone(tz) if tz else late
            self.assertEqual(oddsapi.local_today(), "2026-10-25")


class AltLinesUntilTest(unittest.TestCase):
    def test_override_until_time(self):
        with mock.patch.object(oddsapi, "credits_ok", return_value=False):
            with mock.patch.dict(os.environ, {"LINEMAKER_ALT_LINES_UNTIL": ""}):
                self.assertFalse(oddsapi.alt_lines_ok())
            with mock.patch.dict(os.environ, {"LINEMAKER_ALT_LINES_UNTIL": "2999-01-01 23:59"}):
                self.assertTrue(oddsapi.alt_lines_ok())
            with mock.patch.dict(os.environ, {"LINEMAKER_ALT_LINES_UNTIL": "2001-01-01 23:59"}):
                self.assertFalse(oddsapi.alt_lines_ok())
            with mock.patch.dict(os.environ, {"LINEMAKER_ALT_LINES_UNTIL": "tonight"}):
                self.assertFalse(oddsapi.alt_lines_ok())


class AutoBudgetTest(unittest.TestCase):
    def test_paces_remaining_over_days_left(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {
                "LINEMAKER_CACHE_DIR": d, "LINEMAKER_DAILY_CREDITS": "auto", "LINEMAKER_CREDIT_RESET_DAY": "21"}):
            self.assertEqual(oddsapi.days_to_reset(dt.date(2026, 10, 26)), 26)
            self.assertEqual(oddsapi.days_to_reset(dt.date(2026, 11, 20)), 1)
            self.assertEqual(oddsapi.days_to_reset(dt.date(2026, 11, 21)), 30)
            self.assertEqual(oddsapi.daily_budget(), 700)                 # no balance seen yet today
            oddsapi.record_spend(10, 5990)                                # first call: 6,000 at the start
            with mock.patch.object(oddsapi, "days_to_reset", return_value=20):
                self.assertEqual(oddsapi.daily_budget(), 300)
                oddsapi.record_spend(400, 5590)
                self.assertFalse(oddsapi.credits_ok())                     # 410 spent of 300


class NewsInScansTest(unittest.TestCase):
    def test_cached_rows_reprojected_after_news(self):
        import time
        from linemaker import chat
        rows = [{"sport": "NBA", "game": "New York Knicks @ Boston Celtics", "bet_type": "prop", "subject": "Out Guy",
                 "market": "points", "mean": "20", "stdev": "5"},
                {"sport": "NBA", "game": "New York Knicks @ Boston Celtics", "bet_type": "prop", "subject": "Teammate",
                 "market": "points", "mean": "15", "stdev": "5"},
                {"sport": "NBA", "game": "Other @ Game", "bet_type": "prop", "subject": "Elsewhere",
                 "market": "points", "mean": "10", "stdev": "3"}]
        hit = (time.time() - 600, rows, [], 100, None, "ESPN")
        fresh = [{"subject": "Teammate", "market": "points", "mean": "18.5", "stdev": "5.2", "notes": "usage bump"}]
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LINEMAKER_CACHE_DIR": d}), \
                mock.patch("linemaker.espn_projections.espn_projections", return_value=fresh):
            chat.record_news("NBA", "2026-10-26", "New York Knicks @ Boston Celtics", "Out Guy ruled OUT")
            out = chat._apply_news("NBA", "2026-10-26", "props", hit)[1]
            names = {r["subject"]: r for r in out}
            self.assertNotIn("Out Guy", names)                              # ruled out: gone
            self.assertEqual(names["Teammate"]["mean"], "18.5")             # re-projected
            self.assertEqual(names["Elsewhere"]["mean"], "10")              # other games untouched
            note = chat.news_note("NBA", "2026-10-26", {chat.norm("New York Knicks @ Boston Celtics")})
            self.assertIn("Out Guy ruled OUT", note)
