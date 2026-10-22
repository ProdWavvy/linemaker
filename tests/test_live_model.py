import datetime as dt
import os
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from linemaker import game_model, live_model
from linemaker.live_model import GameState
from linemaker.oddsapi import FeedMarket
from linemaker.pro import ProConfig
from tests.test_game_model import LAST, NOW
from tests.test_oddsapi import FakeAPI

TZ = ZoneInfo("America/New_York")


def board(state, period, clock, home=60, away=50, detail="", situation=None):
    comp = {"status": {"period": period, "displayClock": clock,
                       "type": {"state": state, "shortDetail": detail}},
            "competitors": [{"homeAway": "home", "score": str(home), "team": {"displayName": "Home Hawks"}},
                            {"homeAway": "away", "score": str(away), "team": {"displayName": "Road Bulls"}}]}
    if situation:
        comp["situation"] = situation
    return {"events": [{"competitions": [comp]}]}


def state(sport="NBA", left=0.5, home=60, away=50, h_left=None, a_left=None):
    return GameState(sport, "Road Bulls", "Home Hawks", away, home, "in", "Q3 0:00", left,
                     left if h_left is None else h_left, left if a_left is None else a_left)


class ClockTest(unittest.TestCase):
    def test_time_left(self):
        self.assertAlmostEqual(live_model.time_left("NBA", 3, "12:00"), 0.5)
        self.assertAlmostEqual(live_model.time_left("NBA", 4, "2:24"), 2.4 / 48)
        self.assertAlmostEqual(live_model.time_left("NHL", 2, "10:00"), 30 / 60)
        self.assertAlmostEqual(live_model.time_left("NBA", 5, "5:00"), 5 / 48)   # overtime

    def test_mlb_innings(self):
        self.assertEqual(live_model.mlb_left("Top 9th", 9, 2), (1 / 27, 1 / 9))
        self.assertEqual(live_model.mlb_left("Mid 5th", 5, 0), (4 / 9, 5 / 9))
        self.assertEqual(live_model.mlb_left("End 8th", 8, 0), (1 / 9, 1 / 9))

    def test_parse_scoreboard(self):
        g = live_model.parse_scoreboard("NBA", board("in", 3, "6:00", detail="Q3 6:00"))
        s = g[("road bulls", "home hawks")]
        self.assertEqual((s.lead, s.state), (10, "in"))
        self.assertAlmostEqual(s.left, 18 / 48)
        m = live_model.parse_scoreboard("MLB", board("in", 7, "0:00", 3, 2, "Bot 7th", {"outs": 1}))
        s = m[("road bulls", "home hawks")]
        self.assertAlmostEqual(s.home_left, (2 + 2 / 3) / 9)


class ModelTest(unittest.TestCase):
    def test_basketball_random_walk(self):
        x = live_model.project_live(state(left=0.5), prior_margin=-4.0, prior_total=220.0)
        self.assertAlmostEqual(x["spread_mean"], 10 - 2.0)
        self.assertAlmostEqual(x["margin_sd"], game_model.SD["NBA"] * 0.5 ** 0.5)
        self.assertGreater(x["win_prob"], 0.7)
        # 110 points at halftime = 220 pace: total stays near 220.
        self.assertAlmostEqual(x["total_mean"], 220.0)

    def test_hockey_counts_goals(self):
        late = state("NHL", left=0.05, home=3, away=2)
        x = live_model.project_live(late, prior_margin=0.0, prior_total=6.0)
        self.assertGreater(x["win_prob"], 0.8)
        tied = live_model.project_live(state("NHL", left=0.05, home=2, away=2), 0.0, 6.0)
        self.assertAlmostEqual(tied["win_prob"], 0.5, places=6)
        # The engine's normal curve gives back the counted win probability.
        from statistics import NormalDist
        self.assertAlmostEqual(1 - NormalDist(x["ml_mean"], x["margin_sd"]).cdf(0), x["win_prob"])

    def test_market_prior(self):
        rows = [{"subject": "Home Hawks", "bet_type": "spread", "line": "-3.5", "game": "Road Bulls @ Home Hawks",
                 "books": "FanDuel:-110/-110"},
                {"subject": "Home Hawks", "bet_type": "total", "line": "228.5", "game": "Road Bulls @ Home Hawks",
                 "books": "FanDuel:-110/-110"}]
        m, t = live_model.market_prior(rows, "Home Hawks", "NBA")
        self.assertAlmostEqual(m, 3.5)
        self.assertAlmostEqual(t, 228.5)

    def test_stale_books_dropped(self):
        now = dt.datetime(2026, 10, 21, 23, 0, tzinfo=dt.timezone.utc)
        fm = FeedMarket("2026-10-21", "NBA", "g", "A @ H", "", "moneyline", "H", "A", "moneyline", 0.0,
                        books=[("FanDuel", [-150, 130]), ("DraftKings", [-140, 120])],
                        updated={"FanDuel": "2026-10-21T22:59:30Z", "DraftKings": "2026-10-21T22:50:00Z"})
        out = live_model.drop_stale_books([fm], now)
        self.assertEqual([b for b, _ in out[0].books], ["FanDuel"])


class RunLiveTest(unittest.TestCase):
    def test_end_to_end(self):
        from linemaker import live
        tables = {2027: game_model.parse_standings(NOW), 2026: game_model.parse_standings(LAST)}
        states = {("road bulls", "home hawks"): state(left=0.5, home=62, away=48)}
        with mock.patch.object(game_model, "standings", lambda s, y: tables[y]), \
                mock.patch.object(game_model, "played_yesterday", lambda s, d: set()), \
                mock.patch.object(live_model, "live_states", lambda s, d: states), \
                mock.patch.dict(os.environ, {"LINEMAKER_LIVE_MAX_ODDS_AGE": "1e12"}):
            lr = live.run_live_games(FakeAPI(), ["NBA"], "2026-10-21", TZ, ProConfig())
        self.assertEqual(lr.result.kind, "live")
        self.assertEqual({ev.market.bet_type for ev in lr.result.evaluated}, {"moneyline", "spread", "total"})
        ev = lr.result.evaluated[0]
        self.assertTrue(ev.market.notes.startswith("LIVE · Road Bulls 48, Home Hawks 62"))
        self.assertAlmostEqual(ev.model_weight, live_model.live_weight())
        # Games not in progress are left out entirely.
        with mock.patch.object(live_model, "live_states", lambda s, d: {}):
            lr = live.run_live_games(FakeAPI(), ["NBA"], "2026-10-21", TZ, ProConfig())
        self.assertEqual(lr.result.evaluated, [])

    def test_after_midnight_includes_last_nights_games(self):
        from linemaker import live
        et = ZoneInfo("America/New_York")
        late = dt.datetime(2026, 10, 22, 0, 30, tzinfo=et)
        self.assertEqual(live.live_dates("2026-10-22", et, late), ["2026-10-21", "2026-10-22"])
        self.assertEqual(live.live_dates("2026-10-22", et, late.replace(hour=19)), ["2026-10-22"])
        tables = {2027: game_model.parse_standings(NOW), 2026: game_model.parse_standings(LAST)}
        states = {("road bulls", "home hawks"): state(left=0.3, home=80, away=70)}
        with mock.patch.object(game_model, "standings", lambda s, y: tables[y]), \
                mock.patch.object(game_model, "played_yesterday", lambda s, d: set()), \
                mock.patch.object(live_model, "live_states", lambda s, d: states if d == "2026-10-21" else {}), \
                mock.patch.object(live, "live_dates", lambda d, tz: ["2026-10-21", "2026-10-22"]), \
                mock.patch.dict(os.environ, {"LINEMAKER_LIVE_MAX_ODDS_AGE": "1e12"}):
            lr = live.run_live_games(FakeAPI(), ["NBA"], "2026-10-22", TZ, ProConfig())
        self.assertTrue(lr.result.evaluated)          # the Oct 21 game still in progress is priced
        self.assertTrue(all(ev.market.date == "2026-10-22" for ev in lr.result.evaluated))

    def test_quick_path_words(self):
        from linemaker.chat import quick_kind, quick_plays_request
        self.assertEqual(quick_plays_request("any live edges right now"), ["NBA"])
        self.assertEqual(quick_plays_request("best live nhl bets"), ["NHL"])
        self.assertEqual(quick_kind("any live edges right now"), "live")
        self.assertEqual(quick_kind("best in-game moneylines"), "live")
        self.assertEqual(quick_kind("best nba props today"), "props")


if __name__ == "__main__":
    unittest.main()
