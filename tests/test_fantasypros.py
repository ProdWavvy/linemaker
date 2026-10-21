"""FantasyPros parsing and the auto pipeline, against fixtures shaped like the v2 spec."""
import datetime as dt
import json
import os
import unittest
from zoneinfo import ZoneInfo

from linemaker.fantasypros import (FantasyPros, injury_status, parse_mlb, parse_nba,
                                   parse_nfl, season_for)
from linemaker.live import lock_near_start, run_auto
from linemaker.pro import ProConfig
from tests.test_oddsapi import FakeAPI

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
TZ = ZoneInfo("America/New_York")


def load(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


class FakeFP(FantasyPros):
    def __init__(self):
        super().__init__(api_key="test")
        self.calls = []

    def _get(self, path, **params):
        self.calls.append((path, params))
        if path.endswith("/injuries"):
            return load("fp_nba_injuries.json")
        if path.startswith("/nba/"):
            return load("fp_nba_daily.json")
        if path.startswith("/nfl/"):
            return load("fp_nfl_week.json")
        return load("fp_mlb_pitchers.json" if params.get("position") == "SP"
                    else "fp_mlb_hitters.json")


class ParseTest(unittest.TestCase):
    def test_nba_markets(self):
        rows = {r["market"]: float(r["mean"]) for r in parse_nba(load("fp_nba_daily.json"))
                if r["subject"] == "Jose Example"}
        self.assertEqual(rows["points"], 27.1)
        self.assertEqual(rows["threes"], 2.6)
        self.assertAlmostEqual(rows["PRA"], 39.7)
        # Zero or missing projections are skipped, not priced.
        bench = [r for r in parse_nba(load("fp_nba_daily.json")) if r["subject"] == "Bench Example"]
        self.assertEqual([r["market"] for r in bench], ["assists"])

    def test_nfl_anytime_td_is_rush_plus_rec(self):
        rows = {r["market"]: float(r["mean"]) for r in parse_nfl(load("fp_nfl_week.json"))}
        self.assertAlmostEqual(rows["anytime td"], 0.83)
        self.assertEqual(rows["rushing yards"], 92.3)
        # Stats as a one-element list (the schema's other shape) parse the same.
        alt = load("fp_nfl_week.json")
        alt["players"][0]["stats"] = [alt["players"][0]["stats"]]
        self.assertEqual(len(parse_nfl(alt)), len(rows))

    def test_mlb_total_bases_and_strikeouts(self):
        hit = {r["market"]: float(r["mean"]) for r in parse_mlb(load("fp_mlb_hitters.json"), False)}
        self.assertAlmostEqual(hit["total bases"], 0.70 + 0.48 + 0.15 + 0.76)
        k = parse_mlb(load("fp_mlb_pitchers.json"), True)
        self.assertEqual((k[0]["market"], float(k[0]["mean"])), ("strikeouts", 7.4))

    def test_injuries_and_seasons(self):
        st = injury_status(load("fp_nba_injuries.json"))
        self.assertEqual(st["jose example"], "questionable")
        self.assertEqual(st["hurt example"], "out")
        self.assertEqual(season_for("NBA", "2026-10-21"), 2026)
        self.assertEqual(season_for("NBA", "2027-02-01"), 2026)
        self.assertEqual(season_for("NFL", "2027-01-10"), 2026)
        self.assertEqual(season_for("MLB", "2026-07-04"), 2026)


class AutoTest(unittest.TestCase):
    def test_projections_to_priced_play(self):
        fp, odds = FakeFP(), FakeAPI()
        lr = run_auto(odds, fp, ["NBA"], "2026-10-21", TZ, ProConfig(n_sims=3000))
        self.assertTrue(any(c[0] == "/nba/2026/projections" and c[1]["type"] == "daily"
                            for c in fp.calls))
        priced = [r for r in lr.rows if r["market"] == "points"]
        self.assertEqual(len(priced), 1)
        self.assertEqual(priced[0]["injury_status"], "questionable")
        # Players without a posted line are not listed as passes.
        self.assertFalse(any("no odds found" in r for _, r in lr.result.passes))

    def test_lock_near_start(self):
        now = dt.datetime(2026, 10, 21, 23, 10, tzinfo=dt.timezone.utc)
        rows = [{"commence": "2026-10-21T23:30:00Z"}, {"commence": "2026-10-22T02:00:00Z"},
                {"commence": "2026-10-21T23:30:00Z", "lineup_confirmed": "false"}]
        lock_near_start(rows, now)
        self.assertEqual([r.get("lineup_confirmed") for r in rows], ["true", None, "false"])


if __name__ == "__main__":
    unittest.main()


class SampleDataWarningTest(unittest.TestCase):
    def test_wrong_date_flags_sample_data(self):
        from linemaker.fantasypros import fetch_projections

        class Stale(FakeFP):
            def _get(self, path, **params):
                data = super()._get(path, **params)
                if path.startswith("/nba/") and "projections" in path:
                    data = dict(data, date="2025-05-19")
                return data
        fp = Stale()
        fetch_projections(fp, "NBA", "2026-10-21")
        self.assertIn("not 2026-10-21", fp.warning)

    def test_thin_slate_flagged(self):
        from linemaker.fantasypros import fetch_projections
        fp = FakeFP()
        fetch_projections(fp, "NBA", "2026-10-21")
        self.assertIn("only 2 NBA player projections", fp.warning)
