"""Feed parsing against fixtures shaped like The Odds API v4 docs (no network)."""
import json
import os
import unittest
from zoneinfo import ZoneInfo

from linemaker.live import board_lines, run_live
from linemaker.oddsapi import OddsAPI, merge, norm, parse_game_odds, slate_window
from linemaker.pro import ProConfig

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
TZ = ZoneInfo("America/New_York")


def load(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


class FakeAPI(OddsAPI):
    def __init__(self):
        super().__init__(api_key="test")
        self.calls = []

    def _get(self, path, **params):
        self.calls.append((path, params))
        self.quota.remaining = 400
        if path.endswith("/events/evt1/odds"):
            return load("oddsapi_nba_props_evt1.json")
        if "soccer" in path:
            return load("oddsapi_ucl_odds.json")
        return load("oddsapi_nba_odds.json")


class ParseTest(unittest.TestCase):
    def test_game_lines(self):
        feed = parse_game_odds(load("oddsapi_nba_odds.json"), "NBA", TZ)
        today = [f for f in feed if f.date == "2026-10-21"]
        kinds = sorted((f.bet_type, f.line) for f in today)
        # Two different total lines from two books become two markets.
        self.assertEqual(kinds, [("moneyline", 0.0), ("spread", -3.5), ("total", 228.5), ("total", 229.5)])
        ml = next(f for f in today if f.bet_type == "moneyline")
        self.assertEqual(ml.subject, "Home Hawks")
        self.assertEqual(ml.books, [("DraftKings", [-150, 130]), ("FanDuel", [-145, 124])])
        sp = next(f for f in today if f.bet_type == "spread")
        self.assertEqual(sp.books[1], ("FanDuel", [-105, -115]))

    def test_soccer_three_way(self):
        feed = parse_game_odds(load("oddsapi_ucl_odds.json"), "UCL", TZ)
        mr = next(f for f in feed if f.bet_type == "1x2")
        self.assertEqual(mr.books, [("DraftKings", [105, 240, 250])])
        self.assertEqual(next(f for f in feed if f.bet_type == "total").market, "total goals")

    def test_local_date_window(self):
        start, end = slate_window("2026-10-21", TZ)
        self.assertEqual((start, end), ("2026-10-21T04:00:00Z", "2026-10-22T04:00:00Z"))

    def test_name_normalisation(self):
        self.assertEqual(norm("José Example Jr."), norm("jose example"))


class LiveTest(unittest.TestCase):
    def test_merge_and_run(self):
        api = FakeAPI()
        projections = [
            {"sport": "NBA", "subject": "Jose Example", "market": "points", "mean": "25.1",
             "stdev": "6.5", "lineup_confirmed": "true"},
            {"sport": "NBA", "subject": "Home Hawks", "market": "spread", "mean": "6.0",
             "lineup_confirmed": "true"},
            {"sport": "NBA", "subject": "Nobody", "market": "points", "mean": "10"},
        ]
        lr = run_live(api, ["NBA"], projections, "2026-10-21", TZ, ProConfig(n_sims=4000))
        self.assertEqual(len(lr.rows), 2)
        self.assertEqual(len(lr.missing), 1)
        self.assertTrue(any(c[0].endswith("/events/evt1/odds") for c in api.calls))
        self.assertFalse(any("evt2" in c[0] for c in api.calls))   # tomorrow's game skipped
        labels = [e.best.label for e in lr.result.picks]
        self.assertTrue(any("Jose Example" in l or "José" in l for l in labels))
        self.assertTrue(any("no odds found" in r for _, r in lr.result.passes))
        self.assertEqual(lr.remaining, 400)

    def test_game_lines_only_skip_prop_calls(self):
        api = FakeAPI()
        run_live(api, ["NBA"], [{"sport": "NBA", "subject": "Home Hawks", "market": "moneyline",
                                 "mean": "4"}], "2026-10-21", TZ, ProConfig(n_sims=2000))
        self.assertEqual(len(api.calls), 1)

    def test_projection_pinned_to_line(self):
        feed = parse_game_odds(load("oddsapi_nba_odds.json"), "NBA", TZ)
        rows, _ = merge(feed, [{"sport": "NBA", "subject": "Home Hawks", "market": "game total",
                                "line": "229.5", "mean": "231"}])
        self.assertEqual([r["line"] for r in rows], ["229.5"])

    def test_board(self):
        feed = parse_game_odds(load("oddsapi_nba_odds.json"), "NBA", TZ)
        lines = board_lines(feed)
        self.assertTrue(any("Home Hawks" in d for _, t, d, _ in lines if t == "moneyline"))


if __name__ == "__main__":
    unittest.main()


class TemplateRoundTripTest(unittest.TestCase):
    def test_filled_template_is_a_projections_file(self):
        import io
        from linemaker.live import odds_template, read_projections, rows_csv
        rows = odds_template(FakeAPI(), ["NBA"], "2026-10-21", TZ)
        rows[0]["mean"] = "5.0"          # user fills one row, leaves the rest blank
        proj = read_projections(io.StringIO(rows_csv(rows)))
        self.assertEqual(len(proj), 1)
        lr = run_live(FakeAPI(), ["NBA"], proj, "2026-10-21", TZ, ProConfig(n_sims=2000))
        self.assertEqual(len(lr.rows), 1)


class DocsExamplesTest(unittest.TestCase):
    """The example responses published in the v4 guide, parsed verbatim."""

    def test_docs_odds_example(self):
        feed = parse_game_odds(load("docs_v4_odds_nfl.json"), "NFL", TZ)
        ml = next(f for f in feed if f.bet_type == "moneyline")
        self.assertEqual((ml.subject, ml.opponent), ("Tampa Bay Buccaneers", "Dallas Cowboys"))
        self.assertIn(("Unibet", [-303, 240]), ml.books)
        sp = next(f for f in feed if f.bet_type == "spread" and f.line == -6.5)
        self.assertIn(("Unibet", [-111, -109]), sp.books)
        self.assertGreater(len(ml.books), 5)

    def test_docs_event_props_example(self):
        from linemaker.oddsapi import parse_event_props
        props = parse_event_props(load("docs_v4_event_odds_nfl.json"), "NFL", TZ)
        blough = next(p for p in props if p.subject == "David Blough")
        self.assertEqual((blough.market, blough.line), ("passing tds", 0.5))
        self.assertEqual(blough.books[0], ("DraftKings", [-205, 150]))
        self.assertEqual(len(blough.books), 2)

    def test_docs_scores_example(self):
        from linemaker.oddsapi import results_from_scores
        records = {
            "a": {"game_id": "572d984e132eddaac3da93e5db332e7e", "subject": "Sacramento Kings",
                  "market": "moneyline", "inputs": {"bet_type": "moneyline", "line": 0.0}},
            "b": {"game_id": "572d984e132eddaac3da93e5db332e7e", "subject": "Sacramento Kings",
                  "market": "game total", "inputs": {"bet_type": "total", "line": 220.5}},
            "c": {"game_id": "572d984e132eddaac3da93e5db332e7e", "subject": "X",
                  "market": "points", "inputs": {"bet_type": "prop", "line": 20.5}},
        }
        rows = {r["market"]: r["actual"] for r in
                results_from_scores(load("docs_v4_scores_nba.json"), records)}
        self.assertEqual(rows, {"moneyline": "10", "game total": "216"})


class CreditSavingTest(unittest.TestCase):
    def test_props_only_uses_free_events_endpoint(self):
        api = FakeAPI()
        api._get_orig = api._get

        def _get(path, **params):
            api.calls.append((path, params))
            if path.endswith("/events"):
                return [e for e in load("oddsapi_nba_odds.json")]
            return FakeAPI._get(api, path, **params)
        api._get = _get
        run_live(api, ["NBA"], [{"sport": "NBA", "subject": "Jose Example", "market": "points",
                                 "mean": "25"}], "2026-10-21", TZ, ProConfig(n_sims=2000))
        paths = [c[0] for c in api.calls]
        self.assertIn("/sports/basketball_nba/events", paths)
        self.assertNotIn("/sports/basketball_nba/odds", paths)

    def test_retries_on_429(self):
        import io as _io
        import urllib.error
        from unittest import mock
        api = OddsAPI(api_key="k")
        api.retry_wait = 0
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b"[]"
        ok.__enter__.return_value.headers = {"x-requests-remaining": "99"}
        err = urllib.error.HTTPError("u", 429, "Too Many", {}, _io.BytesIO(b""))
        with mock.patch("urllib.request.urlopen", side_effect=[err, ok]) as m:
            self.assertEqual(api.sports(), [])
        self.assertEqual(m.call_count, 2)
        self.assertEqual(api.quota.remaining, 99)


class AwayTeamProjectionTest(unittest.TestCase):
    def test_away_team_spread_is_restated_for_home(self):
        feed = parse_game_odds(load("oddsapi_nba_odds.json"), "NBA", TZ)
        # Road Bulls +3.5 (away), projected to lose by 1 -> home margin +1 at home line -3.5
        rows, missing = merge(feed, [{"sport": "NBA", "subject": "Road Bulls", "market": "spread",
                                      "line": "3.5", "mean": "-1"}])
        self.assertEqual(missing, [])
        self.assertEqual((rows[0]["subject"], rows[0]["line"], rows[0]["mean"]),
                         ("Home Hawks", "-3.5", "1"))

    def test_away_team_1x2_swaps_goals(self):
        feed = parse_game_odds(load("oddsapi_ucl_odds.json"), "UCL", TZ)
        rows, _ = merge(feed, [{"sport": "UCL", "subject": "Example United", "market": "match result",
                                "mean": "1.4", "mean_b": "0.9"}])
        self.assertEqual((rows[0]["mean"], rows[0]["mean_b"]), ("0.9", "1.4"))


class KeyFailoverTest(unittest.TestCase):
    def setUp(self):
        from linemaker import oddsapi
        self.mod = oddsapi
        oddsapi._ACTIVE_KEY["index"] = 0

    def tearDown(self):
        self.mod._ACTIVE_KEY["index"] = 0

    def _ok(self, remaining):
        from unittest import mock
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b"[]"
        ok.__enter__.return_value.headers = {"x-requests-remaining": str(remaining)}
        return ok

    def test_keys_from_env(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"ODDS_API_KEY": "a", "ODDS_API_KEY_2": "b",
                                          "ODDS_API_KEY_3": "c"}, clear=False):
            os.environ.pop("ODDS_API_KEYS", None)
            self.assertEqual(self.mod.configured_keys(), ["a", "b", "c"])

    def test_401_switches_key_and_retries(self):
        import io as _io
        import urllib.error
        from unittest import mock
        used = []
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {},
                                     _io.BytesIO(b'{"error_code":"OUT_OF_USAGE_CREDITS"}'))

        def fake(url, timeout):
            used.append("apiKey=first" in url and "first" or "second")
            return self._ok(250) if len(used) > 1 else (_ for _ in ()).throw(err)
        with mock.patch.dict(os.environ, {"ODDS_API_KEY": "first", "ODDS_API_KEY_2": "second"}):
            os.environ.pop("ODDS_API_KEYS", None)
            api = self.mod.OddsAPI()
            with mock.patch("urllib.request.urlopen", side_effect=fake):
                self.assertEqual(api.sports(), [])
                self.assertEqual(used, ["first", "second"])
                # A new client keeps using the second key.
                self.assertEqual(self.mod.OddsAPI().api_key, "second")

    def test_zero_remaining_moves_to_next_key(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"ODDS_API_KEY": "first", "ODDS_API_KEY_2": "second"}):
            os.environ.pop("ODDS_API_KEYS", None)
            api = self.mod.OddsAPI()
            with mock.patch("urllib.request.urlopen", return_value=self._ok(0)):
                api.sports()
            self.assertEqual(api.api_key, "second")

    def test_single_key_still_raises(self):
        import io as _io
        import urllib.error
        from unittest import mock
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, _io.BytesIO(b"bad key"))
        api = self.mod.OddsAPI(api_key="only")
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(self.mod.OddsAPIError):
                api.sports()


class AltLinesTest(unittest.TestCase):
    def test_alt_ladder_parsed_into_separate_lines(self):
        from unittest import mock
        from linemaker.oddsapi import default_prop_markets, parse_event_props
        self.assertNotIn("player_points_alternate", default_prop_markets("NBA"))   # off by default
        with mock.patch.dict(os.environ, {"LINEMAKER_ALT_LINES": "on"}):
            self.assertIn("player_points_alternate", default_prop_markets("NBA"))
        event = {"id": "e", "commence_time": "2026-10-21T23:30:00Z", "home_team": "H",
                 "away_team": "A", "bookmakers": [{"key": "draftkings", "title": "DraftKings",
                 "markets": [{"key": "player_points_alternate", "outcomes": [
                     {"name": "Over", "description": "Star", "price": -400, "point": 14.5},
                     {"name": "Over", "description": "Star", "price": -250, "point": 19.5}]}]}]}
        props = parse_event_props(event, "NBA", TZ)
        self.assertEqual(sorted(p.line for p in props), [14.5, 19.5])
        self.assertEqual(props[0].books[0][1][1], None)          # Over only

    def test_one_sided_market_is_priced(self):
        from linemaker.pro import ProConfig, rows_to_markets, run_pro
        rows = [{"date": "2026-10-21", "sport": "NBA", "game_id": "g", "game": "A @ H",
                 "bet_type": "prop", "subject": "Star", "market": "points", "line": "14.5",
                 "mean": "24", "books": "DraftKings:-400/|FanDuel:-380/"}]
        res = run_pro(rows_to_markets(rows), "2026-10-21",
                      ProConfig(n_sims=2000, bet_books=("FanDuel", "DraftKings"), track=False))
        self.assertTrue(res.evaluated)
        self.assertTrue(res.evaluated[0].vig_assumed)


class KeyQuotaTest(unittest.TestCase):
    def test_reports_each_key_without_switching(self):
        import io as _io
        import urllib.error
        from unittest import mock
        from linemaker import oddsapi
        oddsapi._ACTIVE_KEY["index"] = 0

        def fake(url, timeout):
            if "apiKey=bad" in url:
                raise urllib.error.HTTPError("u", 401, "x", {}, _io.BytesIO(b"invalid key"))
            ok = mock.MagicMock()
            ok.__enter__.return_value.read.return_value = b"[]"
            remaining = "0" if "apiKey=first" in url else "480"
            ok.__enter__.return_value.headers = {"x-requests-remaining": remaining,
                                                 "x-requests-used": "20"}
            return ok
        env = {"ODDS_API_KEY": "first", "ODDS_API_KEY_2": "second", "ODDS_API_KEY_3": "bad"}
        with mock.patch.dict(os.environ, env), mock.patch("urllib.request.urlopen", side_effect=fake):
            os.environ.pop("ODDS_API_KEYS", None)
            rows = oddsapi.key_quotas()
        self.assertEqual([(r[0], r[2]) for r in rows], [(1, 0), (2, 480), (3, None)])
        self.assertTrue(rows[0][5])                       # key #1 still the active one
        self.assertIn("401", rows[2][4])
        self.assertEqual(oddsapi._ACTIVE_KEY["index"], 0)  # checking quotas never switches keys


class ExchangeLadderTest(unittest.TestCase):
    def test_exchange_only_lines_dropped_shared_lines_merged(self):
        from zoneinfo import ZoneInfo
        from linemaker.oddsapi import parse_event_props
        def bk(title, key, outs):
            return {"key": title.lower(), "title": title, "markets": [{"key": key, "outcomes": outs}]}
        over = lambda p, l, price: {"name": "Over", "description": p, "point": l, "price": price}
        under = lambda p, l, price: {"name": "Under", "description": p, "point": l, "price": price}
        event = {"id": "e1", "home_team": "H", "away_team": "A", "commence_time": "2026-10-31T23:00:00Z",
                 "bookmakers": [
                     bk("FanDuel", "player_points", [over("P", 20.5, -110), under("P", 20.5, -110)]),
                     bk("Kalshi", "player_points_alternate", [over("P", 20.5, -105), under("P", 20.5, -105),
                                                              over("P", 24.5, 200), under("P", 24.5, -250),
                                                              over("P", 29.5, 500), under("P", 29.5, -700)])]}
        fms = parse_event_props(event, "NBA", ZoneInfo("America/New_York"))
        self.assertEqual([fm.line for fm in fms], [20.5])                      # Kalshi's own ladder gone
        self.assertEqual(sorted(b for b, _p in fms[0].books), ["FanDuel", "Kalshi"])   # its 20.5 merged in
