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


class SavingsTest(unittest.TestCase):
    def test_reserve_holds_budget_for_pretip(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {
                "LINEMAKER_CACHE_DIR": d, "LINEMAKER_DAILY_CREDITS": "1000", "LINEMAKER_PRETIP_RESERVE": "0.3"}):
            oddsapi.record_spend(750)
            self.assertFalse(oddsapi.credits_ok())                         # asks stop at 700
            tok = oddsapi.SPEND_SOURCE.set("pre-tip scans & closing reads")
            self.assertTrue(oddsapi.credits_ok())                          # pre-tip may use the rest
            oddsapi.SPEND_SOURCE.reset(tok)

    def test_market_keys_and_closing_read_skips(self):
        from linemaker import chat
        keys = oddsapi.market_keys_for("NBA", {"points"})
        self.assertIn("player_points", keys)
        self.assertIn("player_points_alternate", keys)
        self.assertNotIn("player_rebounds", keys)
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LINEMAKER_TRACKING_DIR": d}), \
                mock.patch("linemaker.live.run_best") as rb:
            msgs, res = chat.scan_events("NBA", "2026-10-26", {"e1"}, "NBA closing read", True, True)
            self.assertIsNone(res)
            rb.assert_not_called()                                         # nothing played: no credits

    def test_pretip_reuses_fresh_ask_odds(self):
        import time
        from linemaker import chat
        rows = [{"date": "2026-10-26", "sport": "NBA", "game_id": "e1", "game": "A @ H", "bet_type": "prop",
                 "subject": "P", "market": "points", "line": "20.5", "mean": "24", "stdev": "5",
                 "books": "FanDuel:-110/-110|DraftKings:-110/-110"}]
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LINEMAKER_CACHE_DIR": d}), \
                mock.patch.dict(chat._SLATES, {("NBA", "2026-10-26", "props"): (time.time() - 120, rows, [], 0, None, "ESPN")}), \
                mock.patch("linemaker.live.run_best") as rb, mock.patch.object(chat, "enrich", lambda *a, **k: {"profiles": {}, "context": {}, "history": {}}):
            _m, res = chat.scan_events("NBA", "2026-10-26", {"e1"}, "NBA Pre-Tip Scan", False)
            rb.assert_not_called()
            self.assertEqual(len(res.evaluated), 1)


class SpendLabelTest(unittest.TestCase):
    def test_parallel_prop_fetches_keep_the_job_label(self):
        seen = []
        events = [{"id": f"e{i}", "commence_time": "2099-01-01T00:00:00Z", "home_team": "H", "away_team": "A"}
                  for i in range(4)]

        class Api:
            slate = {}
            def events(self, *a):
                return events
        with mock.patch.object(oddsapi, "fetch_event_props",
                               side_effect=lambda *a: seen.append(oddsapi.SPEND_SOURCE.get()) or dict(events[0], bookmakers=[])), \
                mock.patch.object(oddsapi, "_local_date", return_value="2099-01-01"):
            tok = oddsapi.SPEND_SOURCE.set("pre-tip scans & closing reads")
            from zoneinfo import ZoneInfo
            oddsapi.fetch_slate(Api(), "NBA", "2099-01-01", ZoneInfo("UTC"), props=True, game_lines=False)
            oddsapi.SPEND_SOURCE.reset(tok)
        self.assertEqual(seen, ["pre-tip scans & closing reads"] * 4)


class ClosingReadKeysTest(unittest.TestCase):
    def test_buys_only_played_markets(self):
        from linemaker import chat
        from linemaker.jsonio import save_json
        seen = []

        def fake_run_best(*a, **k):
            seen.append(oddsapi.PROP_KEYS.get())
            return NS(result=NS(evaluated=[]))
        from types import SimpleNamespace as NS
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"LINEMAKER_TRACKING_DIR": d}), \
                mock.patch("linemaker.live.run_best", fake_run_best), mock.patch.object(chat, "OddsAPI", lambda: None):
            save_json(os.path.join(d, "NBA-props", "2026-10-27.json"), {
                "k": {"sport": "NBA", "game_id": "e1", "market": "rebounds", "picked": True, "inputs": {"bet_type": "prop"}},
                "j": {"sport": "NBA", "game_id": "e1", "market": "points", "picked": False, "inputs": {"bet_type": "prop"}}})
            chat.scan_events("NBA", "2026-10-27", {"e1"}, "NBA closing read", True, True)
        self.assertEqual(len(seen), 1)
        self.assertIn("player_rebounds", seen[0])
        self.assertNotIn("player_points", seen[0])
        self.assertIsNone(oddsapi.PROP_KEYS.get())                       # reset after the read


class MidnightBudgetTest(unittest.TestCase):
    def test_budget_set_after_midnight_from_every_key(self):
        import tempfile
        from linemaker import oddsapi
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {
                "LINEMAKER_CACHE_DIR": d, "LINEMAKER_DAILY_CREDITS": "auto", "LINEMAKER_CREDIT_RESET_DAY": "21"}), \
                mock.patch.object(oddsapi, "local_today", return_value="2026-10-28"):
            yesterday = oddsapi._spend_path("2026-10-27")
            with open(yesterday, "w") as f:
                json.dump({"total": 300, "remaining": 13042, "start_remaining": 13342}, f)
            self.assertEqual(oddsapi.daily_budget(), 13042 // 24)          # before the check: yesterday's balance
            with mock.patch.object(oddsapi, "key_quotas", return_value=[(1, "…a", 12000, 0, None, True),
                                                                        (2, "…b", 1000, 0, None, False),
                                                                        (3, "…c", None, None, "401", False)]):
                self.assertEqual(oddsapi.refresh_day_budget(), 13000 // 24)   # both keys, the dead one skipped
            oddsapi.record_spend(40, 12960)                                   # a paid call later keeps it
            self.assertEqual(oddsapi._spend("2026-10-28")["start_remaining"], 13000)
