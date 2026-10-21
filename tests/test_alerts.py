import datetime as dt
import os
import tempfile
import unittest
from unittest import mock

from linemaker import alerts

NOW = dt.datetime(2026, 10, 21, 22, 30, tzinfo=dt.timezone.utc)   # 6:30 PM ET


def ev(eid, start, home="Home Hawks", away="Road Bulls"):
    return {"id": eid, "commence_time": start, "home_team": home, "away_team": away}


class PretipTest(unittest.TestCase):
    def test_window_and_grouping(self):
        events = [ev("a", "2026-10-21T23:00:00Z"), ev("b", "2026-10-21T23:00:00Z", "X", "Y"),
                  ev("c", "2026-10-21T23:30:00Z"), ev("d", "2026-10-21T22:33:00Z")]
        groups = alerts.pretip_due(events, NOW, 45, set())
        self.assertEqual([[e["id"] for e in g] for g in groups], [["a", "b"]])   # c too early, d too late
        self.assertEqual(alerts.pretip_due(events, NOW, 45, {"a", "b"}), [])

    def test_off_when_blank(self):
        with mock.patch.dict(os.environ, {"LINEMAKER_PRETIP_MINUTES": ""}):
            self.assertIsNone(alerts.pretip_minutes())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LINEMAKER_PRETIP_MINUTES", None)
            self.assertEqual(alerts.pretip_minutes(), 45)


class NewsTest(unittest.TestCase):
    def test_newly_out(self):
        self.assertEqual(alerts.newly_out({"a": "questionable", "b": "out"},
                                          {"a": "out", "b": "out", "c": "out", "d": "questionable"}),
                         ["a", "c"])

    def test_news_events(self):
        events = [ev("e1", "2026-10-21T23:30:00Z"), ev("e2", "2026-10-21T22:35:00Z", "Late", "Tip")]
        index = {"star guard": ("1", "Home Hawks", "Road Bulls", True),
                 "bench guy": ("2", "Road Bulls", "Home Hawks", False),
                 "late star": ("3", "Late", "Tip", True)}
        roles = {"1": True, "2": False, "3": True}
        with mock.patch("linemaker.gamelogs.roster_index", lambda s, d: index), \
                mock.patch.object(alerts, "rotation_player", lambda s, aid, d: roles[aid]):
            self.assertEqual(alerts.news_events("NBA", "2026-10-21", events, NOW, None, {"star guard": "out"}), [])
            hits = alerts.news_events("NBA", "2026-10-21", events, NOW, {},
                                      {"star guard": "out", "bench guy": "out", "late star": "out"})
        self.assertEqual([(e["id"], p) for e, p in hits], [("e1", ["Star Guard"])])

    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LINEMAKER_CACHE_DIR": tmp}):
            self.assertIsNone(alerts.load_state("x.json", None))
            alerts.save_state("x.json", {"a": "out"})
            self.assertEqual(alerts.load_state("x.json", None), {"a": "out"})


class ScanEventsTest(unittest.TestCase):
    def test_single_game_only_buys_that_game(self):
        from linemaker import chat, oddsapi
        from tests.test_oddsapi import FakeAPI
        api = FakeAPI()
        from zoneinfo import ZoneInfo
        oddsapi.fetch_slate(api, "NBA", "2026-10-21", ZoneInfo("America/New_York"), props=True,
                            game_lines=False, only={"nope"})
        self.assertFalse([p for p, _ in api.calls if p.endswith("/odds")])


if __name__ == "__main__":
    unittest.main()
